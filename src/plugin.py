import asyncio
import json
import time
import logging as log
import multiprocessing
import subprocess
import sys
import webbrowser
import datetime
import dateutil.parser
from yaml import scanner

from galaxy.api.consts import Platform
from galaxy.api.jsonrpc import ApplicationError
from galaxy.api.errors import InvalidCredentials, AuthenticationRequired, AccessDenied, UnknownError
from galaxy.api.plugin import Plugin, create_and_run_plugin
from galaxy.api.types import Authentication, GameTime, Achievement, NextStep, FriendInfo

from backend import BackendClient
from local import LocalParser, ProcessWatcher, GameStatusNotifier, LocalClient
from definitions import GameStatus, System, SYSTEM, UbisoftGame, GameType
from stats import find_times
from consts import AUTH_PARAMS, COOKIES
from games_collection import GamesCollection
from version import __version__
from steam import is_steam_installed
if System.WINDOWS == SYSTEM:
    import ctypes

class UplayPlugin(Plugin):
    def __init__(self, reader, writer, token):
        super().__init__(Platform.Uplay, __version__, reader, writer, token)
        self.client = BackendClient(self)
        self.local_client = LocalClient()
        self.cached_game_statuses = {}
        self.games_collection = GamesCollection()
        self.process_watcher = ProcessWatcher()
        self.game_status_notifier = GameStatusNotifier(self.process_watcher)
        self.tick_count = 0
        self.updating_games = False
        self.owned_games_sent = False
        self.parsing_club_games = False

    def auth_lost(self):
        self.lost_authentication()

    async def authenticate(self, stored_credentials=None):
        if not stored_credentials:
            return NextStep("web_session", AUTH_PARAMS, cookies=COOKIES)
        else:
            try:
                user_data = await self.client.authorise_with_stored_credentials(stored_credentials)
            except (AccessDenied, AuthenticationRequired) as e:
                log.exception(repr(e))
                raise InvalidCredentials()
            except Exception as e:
                log.exception(repr(e))
                raise e
            else:
                self.local_client.initialize(user_data['userId'])
                self.client.set_auth_lost_callback(self.auth_lost)
                return Authentication(user_data['userId'], user_data['username'])

    async def pass_login_credentials(self, step, credentials, cookies):
        """Called just after CEF authentication (called as NextStep by authenticate)"""
        user_data = await self.client.authorise_with_cookies(cookies)
        self.local_client.initialize(user_data['userId'])
        self.client.set_auth_lost_callback(self.auth_lost)
        return Authentication(user_data['userId'], user_data['username'])

    async def get_owned_games(self):
        if not self.client.is_authenticated():
            raise AuthenticationRequired()

        self._parse_local_games()
        self._parse_local_game_ownership()

        await self._parse_club_games()

        self.owned_games_sent = True

        for game in self.games_collection:
            game.considered_for_sending = True

        return [game.as_galaxy_game() for game in self.games_collection
                if game.owned]

    async def _parse_club_games(self):
        if not self.parsing_club_games:
            try:
                self.parsing_club_games = True
                games = await self.client.get_club_titles()
                club_games = []
                for game in games:
                    if "platform" in game:
                        if game["platform"] == "PC":
                            log.info(f"Parsed game from Club Request {game['title']}")
                            club_games.append(
                                UbisoftGame(
                                    space_id=game['spaceId'],
                                    launch_id='',
                                    install_id='',
                                    third_party_id='',
                                    name=game['title'],
                                    path='',
                                    type=GameType.New,
                                    special_registry_path='',
                                    exe='',
                                    status=GameStatus.Unknown,
                                    owned=True
                                ))

                self.games_collection.extend(club_games)
            except ApplicationError as e:
                log.error(f"Encountered exception while parsing club games {repr(e)}")
                raise e
            except Exception as e:
                log.error(f"Encountered exception while parsing club games {repr(e)}")
            finally:
                self.parsing_club_games = False
        else:
            # Wait until club games get parsed if parsing is already in progress
            while self.parsing_club_games:
                await asyncio.sleep(0.2)

    def _parse_local_games(self):
        """Parsing local files should lead to every game having a launch id.
        A game in the games_collection which doesn't have a launch id probably
        means that a game was added through the get_club_titles request but its space id
        was not present in configuration file and we couldn't find a matching launch id for it."""
        try:
            if self.local_client.configurations_accessible():
                configuration_data = self.local_client.read_config()
                p = LocalParser()
                games = []
                for game in p.parse_games(configuration_data):
                    games.append(game)
                self.games_collection.extend(games)
        except scanner.ScannerError as e:
            log.error(f"Scanner error while parsing configuration, yaml is probably corrupted {repr(e)}")

    def _parse_local_game_ownership(self):
        if self.local_client.ownership_accesible():
            ownership_data = self.local_client.read_ownership()
            p = LocalParser()
            ownership_records = p.get_owned_local_games(ownership_data)
            log.info(f"Ownership Records {ownership_records}")
            for game in self.games_collection:
                if game.install_id:
                    if int(game.install_id) in ownership_records:
                        game.owned = True

    def _update_games(self):
        self.updating_games = True
        self._parse_local_games()
        self.updating_games = False

    def _update_local_games_status(self):
        cached_statuses = self.cached_game_statuses
        if cached_statuses is None:
            return

        for game in self.games_collection:
            if game.install_id in cached_statuses:
                self.game_status_notifier.update_game(game)
                if game.status != cached_statuses[game.install_id]:
                    log.info(f"Game {game.name} path changed: updating status from {cached_statuses[game.install_id]} to {game.status}")
                    self.update_local_game_status(game.as_local_game())
                    self.cached_game_statuses[game.install_id] = game.status
            else:
                self.game_status_notifier.update_game(game)
                ''' If a game wasn't previously in a cache then and it appears with an installed or running status
                 it most likely means that client was just installed '''
                if game.status in [GameStatus.Installed, GameStatus.Running]:
                    self.update_local_game_status(game.as_local_game())
                self.cached_game_statuses[game.install_id] = game.status

    async def get_local_games(self):
        self._parse_local_games()

        local_games = []

        for game in self.games_collection:
            self.cached_game_statuses[game.launch_id] = game.status
            if game.status == GameStatus.Installed or game.status == GameStatus.Running:
                local_games.append(game.as_local_game())
        self._update_local_games_status()
        return local_games

    async def _add_new_games(self, games):
        await self._parse_club_games()
        self._parse_local_game_ownership()
        for game in games:
            if game.owned:
                self.add_game(game.as_galaxy_game())

    async def get_game_times(self):
        """This method is required to inform Galaxy that game time feature is supported.
        Probably galaxy.api bug as only `import_game_times` is mentioned in documentation"""

    async def import_game_times(self, game_ids):
        def no_stats_for_game(game_id):
            self.game_time_import_success(GameTime(game_id, None, None))

        if not self.client.is_authenticated():
            raise AuthenticationRequired()

        blacklist = json.loads(self.persistent_cache.get('games_without_stats', '{}'))
        current_time = int(time.time())

        for game_id in game_ids:
            try:
                expire_in = blacklist.get(game_id, 0) - current_time
                if expire_in > 0:
                    log.debug(f'Cache: No game stats for {game_id}. Recheck in {expire_in}s')
                    no_stats_for_game(game_id)
                    continue

                game = self.games_collection[game_id]
                if not game.space_id:
                    no_stats_for_game(game_id)
                    continue

                try:
                    response = await self.client.get_game_stats(game.space_id)
                except ApplicationError as err:
                    self.game_time_import_failure(game_id, err)
                    continue

                statscards = response.get('Statscards', None)
                if statscards is None:
                    blacklist[game_id] = current_time + 3600 * 24 * 14  # two weeks
                    no_stats_for_game(game_id)
                    continue

                playtime, last_played = find_times(statscards, game_id)
                log.info(f'Stats for {game.name}: playtime: {playtime}, last_played: {last_played}')
                self.game_time_import_success(GameTime(game_id, playtime, last_played))

            except Exception as e:
                log.error(f"Getting game times for game {game_id} has crashed: " + repr(e))
                self.game_time_import_failure(game_id, UnknownError())

        self.persistent_cache['games_without_stats'] = json.dumps(blacklist)
        self.push_cache()

    async def get_unlocked_challenges(self, game_id):
        """Challenges are a unique uplay club feature and don't directly translate to achievements"""
        if not self.client.is_authenticated():
            raise AuthenticationRequired()
        for game in self.games_collection:
            if game.space_id == game_id or game.launch_id == game_id:
                if not game.space_id:
                    return[]
                challenges = await self.client.get_challenges(game.space_id)
                return [
                    Achievement(achievement_id=challenge["id"], achievement_name=challenge["name"],
                                unlock_time=int(
                                    datetime.datetime.timestamp(dateutil.parser.parse(challenge["completionDate"]))))
                    for challenge in challenges["actions"] if challenge["isCompleted"] and not challenge["isBadge"]
                ]

    async def launch_game(self, game_id):
        if not self.user_can_perform_actions():
            return

        for game in self.games_collection.get_local_games():

            if (game.space_id == game_id or game.install_id == game_id) and game.status == GameStatus.Installed:
                if game.type == GameType.Steam:
                    if is_steam_installed():
                        url = f"start steam://rungameid/{game.third_party_id}"
                    else:
                        url = f"start uplay://open/game/{game.launch_id}"
                elif game.type == GameType.New or game.type == GameType.Legacy:
                    log.debug('Launching legacy game')
                    self.game_status_notifier._legacy_game_launched = True
                    url = f"start uplay://launch/{game.launch_id}"
                else:
                    log.error(f"Unsupported game type {game.name}")
                    self.open_uplay_client()
                    return

                log.info(f"Launching game '{game.name}' by protocol: [{url}]")

                subprocess.Popen(url, shell=True)
                return

        for game in self.games_collection:
            if (game.space_id == game_id or game.install_id == game_id) and game.status in [GameStatus.NotInstalled,
                                                                                            GameStatus.Unknown]:
                log.warning("Game is not installed, installing")
                return await self.install_game(game_id)

        log.info("Failed to launch game, launching client instead.")
        self.open_uplay_client()

    async def install_game(self, game_id):
        log.debug(self.games_collection)
        if not self.user_can_perform_actions():
            return

        for game in self.games_collection:
            if game.owned and (game.space_id == game_id or game.install_id == game_id) and game.status in [GameStatus.NotInstalled,
                                                                                           GameStatus.Unknown]:
                if game.install_id:
                    log.info(f"Installing game: {game_id}, {game}")
                    subprocess.Popen(f"start uplay://install/{game.install_id}", shell=True)
                    return
            if game.owned and (game.space_id == game_id or game.install_id == game_id) and game.status == GameStatus.Installed:
                log.warning("Game already installed, launching")
                return await self.launch_game(game_id)

        # if launch_id is not known, try to launch local client instead
        self.open_uplay_client()
        log.info(
            f"Did not found game with game_id: {game_id}, proper launch_id and NotInstalled status, launching client.")

    async def uninstall_game(self, game_id):
        if not self.user_can_perform_actions():
            return

        for game in self.games_collection.get_local_games():
            if (game.space_id == game_id or game.launch_id == game_id) and game.status == GameStatus.Installed:
                subprocess.Popen(f"start uplay://uninstall/{game.launch_id}", shell=True)
                return

        self.open_uplay_client()
        log.info(
            f"Did not found game with game_id: {game_id}, proper launch_id and Installed status, launching client.")

    def user_can_perform_actions(self):
        if not self.local_client.is_installed:
            self.open_uplay_browser()
            return False
        if not self.local_client.was_user_logged_in:
            self.open_uplay_client()
            return False
        return True

    def open_uplay_client(self):
        subprocess.Popen(f"start uplay://", shell=True)

    def open_uplay_browser(self):
        url = f'https://uplay.ubisoft.com'
        log.info(f"Opening uplay website: {url}")
        webbrowser.open(url, autoraise=True)

    def refresh_game_statuses(self):
        if not self.local_client.was_user_logged_in:
            return
        statuses = self.game_status_notifier.statuses
        new_games = []
        for game in self.games_collection:
            if game.install_id in statuses:
                if statuses[game.install_id] == GameStatus.Installed and game.status in [GameStatus.NotInstalled, GameStatus.Unknown]:
                    log.info(f"updating status for {game.name} to installed from not installed")
                    game.status = GameStatus.Installed
                    self.update_local_game_status(game.as_local_game())
                elif statuses[game.install_id] == GameStatus.Installed and game.status == GameStatus.Running:
                    log.info(f"updating status for {game.name} to installed from running")
                    game.status = GameStatus.Installed
                    self.update_local_game_status(game.as_local_game())
                    asyncio.create_task(self.prevent_uplay_from_showing())
                elif statuses[game.install_id] == GameStatus.Running and game.status != GameStatus.Running:
                    log.info(f"updating status for {game.name} to running")
                    game.status = GameStatus.Running
                    self.update_local_game_status(game.as_local_game())
                elif statuses[game.install_id] in [GameStatus.NotInstalled, GameStatus.Unknown] and game.status not in [GameStatus.NotInstalled, GameStatus.Unknown]:
                    log.info(f"updating status for {game.name} to not installed")
                    game.status = GameStatus.NotInstalled
                    self.update_local_game_status(game.as_local_game())

            if self.owned_games_sent and not game.considered_for_sending:
                game.considered_for_sending = True
                new_games.append(game)

        if new_games:
            asyncio.create_task(self._add_new_games(new_games))

    async def get_friends(self):
        friends = await self.client.get_friends()
        return [
            FriendInfo(user_id=friend["pid"], user_name=friend["nameOnPlatform"])
            for friend in friends["friends"]
        ]

    async def shutdown_platform_client(self):
        log.info("Shutdown platform client called")
        if self.local_client.is_installed:
            subprocess.Popen("taskkill.exe /im \"upc.exe\"", shell=True)

    async def prevent_uplay_from_showing(self):
        client_popup_wait_time = 5
        check_frequency_delay = 0.02

        hwnd = ctypes.windll.user32.FindWindowW(None, "Uplay")
        end_time = time.time() + client_popup_wait_time

        if hwnd:
            try:
                while not ctypes.windll.user32.IsWindowVisible(hwnd):
                    if time.time() >= end_time:
                        log.info("Timed out post close game uplay popup")
                        break
                    await asyncio.sleep(check_frequency_delay)
                    try:
                        hwnd = ctypes.windll.user32.FindWindowW(None, "Uplay")
                    except Exception as e:
                        log.error(f"exception while retrieving window handle {hwnd} {repr(e)}")
                await self.shutdown_platform_client()
            except Exception as e:
                log.error(f"Exception when checking if window is visible {repr(e)}")

    def tick(self):
        loop = asyncio.get_event_loop()
        if SYSTEM == System.WINDOWS:
            self.tick_count += 1
            if self.tick_count % 1 == 0:
                self.refresh_game_statuses()
            if self.tick_count % 5 == 0:
                self.game_status_notifier.launcher_log_path = self.local_client.launcher_log_path
            if self.tick_count % 9 == 0:
                self._update_local_games_status()
                if self.local_client.ownership_changed():
                    if not self.updating_games:
                        log.info('Ownership file has been changed or created. Reparsing.')
                        loop.run_in_executor(None, self._update_games)
        return


    def shutdown(self):
        log.info("Plugin shutdown.")
        asyncio.create_task(self.client.close())


def main():
    multiprocessing.freeze_support()
    create_and_run_plugin(UplayPlugin, sys.argv)


if __name__ == "__main__":
    main()

