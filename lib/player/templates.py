from lib.math.angle_deg import AngleDeg
from lib.player.object_ball import BallObject
from lib.player.object_player import PlayerObject
from lib.rcsc.game_mode import GameMode
from lib.rcsc.game_time import GameTime
from lib.rcsc.types import SideID, GameModeType


class WorldModel:
    def ball(self) -> BallObject: ...

    def self(self) -> PlayerObject: ...

    def our_side(self) -> SideID: ...

    def our_player(self, unum): ...

    def their_player(self, unum): ...

    def time(self) -> GameTime: ...

    def team_name(self) -> str: ...

    def game_mode(self) -> GameMode: ...

    def our_goalie_unum(self) -> int: ...

    def _set_our_goalie_unum(self): ...

    def teammates_from_ball(self): ...

    def opponents_from_ball(self): ...

    def _set_teammates_from_ball(self): ...

    def last_kicker_side(self) -> SideID: ...

    def exist_kickable_opponents(self): ...

    def exist_kickable_teammates(self): ...


class PlayerAgent:
    def do_dash(self, power, angle=0) -> bool: ...

    def do_turn(self, angle) -> bool: ...

    def do_move(self, x, y) -> bool: ...

    def do_kick(self, power: float, rel_dir: AngleDeg) -> bool: ...

    def world(self) -> WorldModel: ...

    def full_world(self) -> WorldModel: ...

    def init_dlog(self, message: str): ...
