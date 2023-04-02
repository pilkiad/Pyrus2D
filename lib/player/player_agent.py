from typing import Union
import logging
import time
from lib.action.kick_table import KickTable
from base.decision import get_decision
from lib.debug.debug import log
from lib.debug.level import Level
from lib.debug.color import Color
from pyrusgeom.angle_deg import AngleDeg
from lib.player.action_effector import ActionEffector
from lib.player.sensor.body_sensor import SenseBodyParser
from lib.player.sensor.see_state import SeeState
from lib.player.sensor.visual_sensor import VisualSensor
from lib.player.soccer_action import ViewAction, NeckAction, FocusPointAction
from lib.player.soccer_agent import SoccerAgent
from lib.player.world_model import WorldModel
from lib.network.udp_socket import IPAddress
from pyrusgeom.soccer_math import min_max
from lib.player_command.player_command import PlayerInitCommand, PlayerByeCommand
from lib.player_command.player_command_support import PlayerDoneCommand, PlayerTurnNeckCommand
from lib.player_command.player_command_sender import PlayerSendCommands
from lib.rcsc.game_mode import GameMode
from lib.rcsc.game_time import GameTime
from lib.rcsc.server_param import ServerParam
from lib.rcsc.types import UNUM_UNKNOWN, GameModeType, SideID, ViewWidth
from lib.messenger.messenger import Messenger
import team_config

DEBUG = True


def get_time_msec():
    return int(time.time() * 1000)


class PlayerAgent(SoccerAgent):
    def __init__(self):
        super().__init__()
        self._think_received = False
        self._current_time: GameTime = GameTime()
        self._last_decision_time: GameTime = GameTime()

        self._sense_body_parser: SenseBodyParser = SenseBodyParser()
        self._visual_sensor: VisualSensor = VisualSensor()
        self._see_state: SeeState = SeeState()

        self._team_name = team_config.TEAM_NAME
        self._game_mode: GameMode = GameMode()
        self._server_cycle_stopped: bool = True

        self._body_time_stamp: Union[int, None] = None

        self._neck_action: Union[NeckAction, None] = None
        self._view_action: Union[ViewAction, None] = None
        self._focus_point_action: Union[FocusPointAction, None] = None

        self._goalie: bool = False

        self._world = WorldModel()
        self._full_world = WorldModel()
        self._last_body_command = []
        self._is_synch_mode = True
        self._effector = ActionEffector(self)
        self._communication = None

    def send_init_command(self):
        # TODO check reconnection

        com = PlayerInitCommand(team_config.TEAM_NAME, team_config.PLAYER_VERSION, self._goalie)
        # TODO set team name from config
        self._full_world._team_name = team_config.TEAM_NAME

        if self._client.send_message(com.str()) <= 0:
            log.os_log().error("ERROR failed to connect to server")
            self._client.set_server_alive(False)

    def send_bye_command(self):
        if self._client.is_server_alive() is True:  # TODO FALSE?
            com = PlayerByeCommand()
            self._client.send_message(com.str())
            self._client.set_server_alive(False)

    def parse_sense_body_message(self, message: str):
        self._body_time_stamp = get_time_msec()

        self.parse_cycle_info(message, True)
        if DEBUG:
            log.os_log().debug(f'##################{self.world().time().cycle()}#################')
        self._sense_body_parser.parse(message, self._current_time)

        if DEBUG:
            log.sw_log().sensor().add_text('===Received Sense Message===\n' + message)
            log.os_log().debug('===Received Sense Message===\n' + message)
            log.sw_log().sensor().add_text(str(self._sense_body_parser))
            log.os_log().debug(str(self._sense_body_parser))

        self._see_state.update_by_sense_body(self._current_time,
                                             self._sense_body_parser.view_width())
        self._effector.check_command_count(self._sense_body_parser)
        self.world().update_world_after_sense_body(self._sense_body_parser, self._effector, self._current_time)
        # TODO CHECK HERE FOR SEPARATE WORLD

        if DEBUG:
            log.sw_log().world().add_text("===Sense Body Results self===\n" + str(self.world().self()))
            log.sw_log().world().add_text("===Sense Body Results ball===\n" + str(self.world().ball()))
            log.os_log().debug("===Sense Body Results self===\n" + str(self.world().self()))
            log.os_log().debug("===Sense Body Results ball===\n" + str(self.world().ball()))

    def visual_parser(self, message: str):
        if ServerParam.i().is_fullstate(self.world().our_side()) and not team_config.FULL_STATE_DEBUG:
            return
        self.parse_cycle_info(message, False)
        log.debug_client().add_message(f'rec see in {self.world().time().cycle()}\n')
        self._visual_sensor.parse(message,
                                  self._team_name,
                                  self._current_time)

        if DEBUG:
            log.sw_log().sensor().add_text('===Received See Message Sensor===\n' + message)
            log.os_log().debug(f'{"=" * 30}See Message Sensor{"=" * 30}\n' + message)
            log.sw_log().sensor().add_text('===Received See Message Visual Sensor===\n' + str(self._visual_sensor))
            log.os_log().debug(f'{"=" * 30}Visual Sensor{"=" * 30}\n' + str(self._visual_sensor))

        self._see_state.update_by_see(self._current_time,
                                      self.world().self().view_width())

        if self._visual_sensor.time() == self._current_time and \
                self.world().see_time() != self._current_time:
            self.world().update_after_see(self._visual_sensor,
                                          self._sense_body_parser,
                                          self.effector(),
                                          self._current_time)

    def hear_parser(self, message: str):
        self.parse_cycle_info(message, False)
        _, cycle, sender = tuple(
            message.split(" ")[:3]
        )
        cycle = int(cycle)

        if sender[0].isnumeric() or sender[0] == '-':  # PLAYER MESSAGE
            self.hear_player_parser(message)
        elif sender == "referee":
            self.hear_referee_parser(message)

    def hear_player_parser(self, message: str):
        if message.find('"') == -1:
            return
        data = message.strip('()').split(' ')
        if len(data) < 6:
            log.os_log().error(f"(hear player parser) message format is not matched! msg={message}")
            return
        player_message = message.split('"')[1]
        if not data[4].isdigit():
            return
        sender = int(data[4])

        Messenger.decode_all(self.world()._messenger_memory,
                             player_message,
                             sender,
                             self._current_time)

    def hear_referee_parser(self, message: str):
        mode = message.split(" ")[-1].strip(")")
        if not self._game_mode.update(mode, self._current_time):
            return

        # TODO CARDS AND OTHER STUFF

        self.update_server_status()

        if self._game_mode.type() is GameModeType.TimeOver:
            self.send_bye_command()
            return
        self.world().update_game_mode(self._game_mode, self._current_time)
        if ServerParam.i().is_fullstate(self.world().our_side()) and team_config.FULL_STATE_DEBUG:
            self.full_world().update_game_mode(self._game_mode, self._current_time)

    def update_server_status(self):
        if self._server_cycle_stopped:
            self._server_cycle_stopped = False

        if self._game_mode.is_server_cycle_stopped_mode():
            self._server_cycle_stopped = True

    def parse_cycle_info(self, msg: str, by_sense_body: bool):
        cycle = int(msg.split(' ')[1].removesuffix(')\x00'))
        self.update_current_time(cycle, by_sense_body)

    def update_current_time(self, new_time: int, by_sense_body: bool):
        old_time: GameTime = self._current_time.copy()

        if self._server_cycle_stopped:
            if new_time != self._current_time.cycle():
                self._current_time.assign(new_time, 0)

                if new_time - 1 != old_time.cycle():
                    log.os_log().warn(f"player({self.world().self_unum()}):"
                                      f"last server time was wrong maybe")
            else:
                if by_sense_body:
                    self._current_time.assign(self._current_time.cycle(), self._current_time.stopped_cycle() + 1)
                    log.sw_log().any().add_text(f"Cycle: {self._current_time.cycle()}-"
                                                f"{self._current_time.stopped_cycle()} " + '-' * 20)

                    if self._last_decision_time != old_time and old_time.stopped_cycle() != 0:
                        log.sw_log().system().add_text(f"(update current time) missed last action(1)")
        else:
            self._current_time.assign(new_time, 0)
            if old_time.cycle() != new_time:
                log.sw_log().any().add_text(f"Cycle {new_time}-0 " + '-' * 20)

                if new_time - 1 != old_time.cycle():
                    log.os_log().warn(f"player({self.world().self_unum()}):"
                                      f"last server time was wrong maybe")

                if (self._last_decision_time.stopped_cycle() == 0
                        and self._last_decision_time.cycle() != new_time - 1):
                    log.sw_log().system().add_text(f"(update current time) missed last action(2)")

    def think_received(self):
        return self._think_received

    def is_decision_time(self, msec_from_sense: int, timeout_count: int):
        SP = ServerParam.i()

        if SP.synch_mode():
            return False

        if msec_from_sense < 0:
            return False

        if self._last_decision_time == self._current_time:
            return False

        if self.world().self().unum() == UNUM_UNKNOWN:
            return False

        if self.world().see_time() == self._current_time:
            return True

        wait_thr: int = (team_config.WAIT_TIME_THR_SYNCH_VIEW
                         if self._see_state.is_synch()
                         else team_config.WAIT_TIME_THR_NOSYNCH_VIEW)

        if self._last_decision_time == self.world().sense_body_time() and timeout_count <= 2:
            return False

        if SeeState.synch_see_mode() and SP.synch_see_offset() > wait_thr and msec_from_sense >= 0:
            return True

        if self._see_state.is_synch() and self._see_state.cycles_till_next_see() > 0:
            return True

        if msec_from_sense >= wait_thr * SP.slow_down_factor():
            return True
        return False

    def do_neck_action(self):
        if self._neck_action:
            self._neck_action.execute(self)
            self._neck_action = None

    def do_view_action(self):
        if self._view_action:
            self._view_action.execute(self)
            self._view_action = None

    def do_change_focus_action(self):
        if self._focus_point_action:
            self._focus_point_action.execute(self)
            self._focus_point_action = None

    def communicate_impl(self):
        if self._communication is not None:
            self._communication.execute(self)

    def init_impl(self, goalie: bool):
        self._goalie = goalie

    def handle_start(self):
        if self._client is None:
            return False

        if team_config.PLAYER_VERSION < 18:
            log.os_log().warn("PYRUS2D base code does not support player version less than 18.")
            self._client.set_server_alive(False)
            return False

        # TODO check for config.host not empty

        if not self._client.connect_to(IPAddress(team_config.HOST, team_config.PLAYER_PORT)):
            log.os_log().error("ERROR failed to connect to server")
            self._client.set_server_alive(False)
            return False

        self.send_init_command()
        return True

    def handle_exit(self):
        if self._client.is_server_alive():
            self.send_bye_command()
        log.os_log().info(f"player( {self._world.self_unum()} ): finished")  # TODO : Not working

    def handle_message(self):
        self.run()

    def see_state(self):
        return self._see_state

    def run(self):
        last_time_rec = time.time()
        waited_msec: int = 0
        timeout_count: int = 0
        while True:
            message_and_address = []
            message_count = 0
            while True:
                self._client.recv_message(message_and_address)
                message = message_and_address[0]
                server_address = message_and_address[1]
                if len(message) != 0:
                    self.parse_message(message.decode())

                    last_time_rec = time.time()
                    waited_msec = 0
                    timeout_count = 0
                    break

                elif time.time() - last_time_rec > 3:
                    self._client.set_server_alive(False)
                    break
                message_count += 1
                if self.think_received():
                    last_time_rec = time.time()
                    break

                waited_msec += team_config.SOCKET_INTERVAL
                timeout_count += 1
                self.handle_timeout(timeout_count, waited_msec)

            if not self._client.is_server_alive():
                log.os_log().info(f"{team_config.TEAM_NAME} Agent : Server Down")
                break

            if self.think_received():
                self.action()
                self.debug_players()
                self._think_received = False
            elif not ServerParam.i().synch_mode():
                if (self._last_decision_time != self._current_time
                        and self.world().see_time() == self._current_time):
                    self.action()  # TODO CHECK

    def handle_timeout(self, timeout_count: int, waited_msec: int):
        msec_from_sense = -1
        if self._body_time_stamp:
            msec_from_sense = get_time_msec() - self._body_time_stamp

        if self.is_decision_time(msec_from_sense, timeout_count):
            self.action()

    def debug_players(self):
        for p in self.world()._teammates + self.world()._opponents + self.world()._unknown_players:
            if p.pos_valid():
                log.sw_log().world().add_circle(1, center=p.pos(), color=Color(string='blue'))
        if self.world().ball().pos_valid():
            log.sw_log().world().add_circle(center=self.world().ball().pos(), r=0.5, color=Color(string="blue"), fill=True)

        if ServerParam.i().is_fullstate(self.world().our_side()) and team_config.FULL_STATE_DEBUG:
            for p in self.full_world()._teammates + self.full_world()._opponents + self.full_world()._unknown_players:
                if p.pos_valid():
                    log.sw_log().world().add_circle(1, center=p.pos(), color=Color(string='red'))
            if self.world().ball().pos_valid():
                log.sw_log().world().add_circle(center=self.world().ball().pos(), r=0.5, color=Color(string="red"),
                                                fill=True)

    def parse_message(self, message: str):
        if message.startswith("(init"):
            self.parse_init(message)
        if message.startswith("(server_param"):
            ServerParam.i().parse(message)
        if message.startswith("(sense_body"):
            self.parse_sense_body_message(message)
            KickTable.instance().create_tables()
        if message.startswith("(see"):
            self.visual_parser(message)
        if message.startswith("(hear"):
            self.hear_parser(message)
        if message.startswith("(fullstate"):
            self._full_world.parse(message)
        if message.startswith("(player_type") or message.startswith("(sense_body") or message.startswith("(init"):
            self._world.parse(message)
            self._full_world.parse(message)
        if message.startswith("(think"):
            self._think_received = True

    def do_dash(self, power, angle=0):
        if self.world().self().is_frozen():
            log.os_log().error(f"(do dash) player({self._world.self_unum()} is frozen!")
            return False
        self._last_body_command.append(self._effector.set_dash(power, float(angle)))
        return True

    def do_turn(self, angle):
        if self.world().self().is_frozen():
            log.os_log().error(f"(do turn) player({self._world.self_unum()} is frozen!")
            return False
        self._last_body_command.append(self._effector.set_turn(float(angle)))
        return True

    def do_move(self, x, y):
        if self.world().self().is_frozen():
            log.os_log().error(f"(do move) player({self._world.self_unum()} is frozen!")
            return False
        self._last_body_command.append(self._effector.set_move(x, y))
        return True

    def do_kick(self, power: float, rel_dir: AngleDeg):
        if self.world().self().is_frozen():
            log.os_log().error(f"(do kick) player({self._world.self_unum()} is frozen!")
            return False
        self._last_body_command.append(self._effector.set_kick(power, rel_dir))
        return True

    def do_tackle(self, power_or_dir: float, foul: bool):
        if self.world().self().is_frozen():
            log.os_log().error(f"(do tackle) player({self._world.self_unum()} is frozen!")
            return False
        self._last_body_command.append(self._effector.set_tackle(power_or_dir, foul))
        return True

    def do_catch(self):
        wm = self.world()
        if wm.self().is_frozen():
            log.os_log().error(f"(do catch) player({self._world.self_unum()} is frozen!")
            return False

        if not wm.self().goalie():
            log.os_log().error(f"(do catch) player({self._world.self_unum()} is not goalie!")
            return False

        if wm.game_mode().type() is not GameModeType.PlayOn and not wm.game_mode().type().is_penalty_taken():
            log.os_log().error(f"(do catch) player({self._world.self_unum()} play mode is not play_on!")
            return False

        if not wm.ball().rpos_valid():
            log.os_log().error(f"(do catch) player({self._world.self_unum()} ball rpos is not valid!")
            return False

        self._last_body_command.append(self.effector().set_catch())

    def do_turn_neck(self, moment: AngleDeg) -> bool:
        self._last_body_command.append(self._effector.set_turn_neck(moment))
        return True

    def do_change_view(self, width: ViewWidth) -> bool:
        self._last_body_command.append(self._effector.set_change_view(width))
        return True

    def do_change_focus(self, moment_dist: float, moment_dir: Union[float, AngleDeg]):
        if isinstance(moment_dir, float) or isinstance(moment_dir, int):
            moment_dir = AngleDeg(moment_dir)

        aligned_moment_dist = moment_dist
        if self.world().self().focus_point_dist() + aligned_moment_dist < 0.0:
            log.os_log().warn(f"(do_change_focus) player({self._world.self_unum()} focus dist can not be less than 0")
            aligned_moment_dist = -self.world().self().focus_point_dist()
        if self.world().self().focus_point_dist() + aligned_moment_dist > 40.0:
            log.os_log().warn(f"(do_change_focus) player({self._world.self_unum()} focus dist can not be more than 40")
            aligned_moment_dist = 40.0 - self.world().self().focus_point_dist()
        next_view = self.effector().queued_next_view_width()
        next_half_angle = next_view.width() * 0.5

        aligned_moment_dir = moment_dir
        focus_point_dir_after_change_view = AngleDeg(
            min_max(-next_half_angle, self.world().self().focus_point_dir().degree(), next_half_angle))
        if focus_point_dir_after_change_view.degree() + aligned_moment_dir.degree() < -next_half_angle:
            aligned_moment_dir = -next_half_angle - focus_point_dir_after_change_view.degree()
        elif focus_point_dir_after_change_view.degree() + aligned_moment_dir.degree() > next_half_angle:
            aligned_moment_dir = next_half_angle - focus_point_dir_after_change_view.degree()

        self._last_body_command.append(self._effector.set_change_focus(aligned_moment_dist, aligned_moment_dir))

        return True

    def add_say_message(self, message: Messenger):
        self._effector.add_say_message(message)

    def do_attentionto(self, side: SideID, unum: int):
        if side is SideID.NEUTRAL:
            log.os_log().error("(player agent do attentionto) side is neutral!")
            return

        if unum == UNUM_UNKNOWN or not (1 <= unum <= 11):
            log.os_log().error(f"(player agent do attentionto) unum is not in range! unum={unum}")
            return

        if self.world().our_side() == side and self.world().self().unum() == unum:
            log.os_log().error(f"(player agent do attentionto) attentioning to self!")
            return

        if self.world().self().attentionto_side() == side and self.world().self().attentionto_unum() == unum:
            log.os_log().error(f"(player agent do attentionto) already attended to the player! unum={unum}")
            return

        self._last_body_command.append(self._effector.set_attentionto(side, unum))

    def do_attentionto_off(self):
        self._last_body_command.append(self._effector.set_attentionto_off())

    if team_config.FULL_STATE_DEBUG:
        def world(self):
            return self._world
    else:
        def world(self) -> WorldModel:
            return self._full_world

    def full_world(self) -> WorldModel:
        return self._full_world

    def effector(self):
        return self._effector

    def action_impl(self):
        pass

    def action(self):
        if (self.world().self_unum() is None
                or self.world().self().unum() != self.world().self_unum()):
            return

        self.world().update_just_before_decision(self._effector, self._current_time)
        if ServerParam.i().is_fullstate(self._world.our_side()) and team_config.FULL_STATE_DEBUG:
            self.full_world().update_just_before_decision(self._effector, self._current_time)

        self._effector.reset()

        self.action_impl()
        self.do_view_action()
        self.do_neck_action()
        self.do_change_focus_action()
        self.communicate_impl()

        self._last_decision_time = self._current_time.copy()

        log.os_log().debug("body " + str(self.world().self().body()))
        log.os_log().debug("pos " + str(self.world().self().pos()))

        self.world().update_just_after_decision(self._effector)
        if DEBUG:
            log.os_log().debug("======Self after decision======")
            log.os_log().debug("turn " + str(self.effector().get_turn_info()))
            log.os_log().debug("dash " + str(self.effector().get_dash_info()))
            log.os_log().debug("next body " + str(self.effector().queued_next_self_body()))
            log.os_log().debug("next pos " + str(self.effector().queued_next_self_pos()))
            # log.os_log().debug(str(self.world().self().long_str()))

        self._see_state.set_view_mode(self.world().self().view_width())

        message_command = self._effector.make_say_message_command(self.world())
        if message_command:
            self._last_body_command.append(message_command)
        commands = self._last_body_command
        # if self.world().our_side() == SideID.RIGHT:
        # PlayerCommandReverser.reverse(commands) # unused :\ # its useful :) # nope not useful at all :(
        if self._is_synch_mode:
            commands.append(PlayerDoneCommand())
        message = self.make_commands(commands)
        log.debug_client().add_message("\nsent message: " + str(message))
        if DEBUG:
            log.os_log().debug("sent message: " + str(message))
        self._client.send_message(message)
        log.debug_client().write_all(self.world(), None)  # TODO add action effector
        log.sw_log().flush()
        self._last_body_command = []
        self._effector.clear_all_commands()

    def make_commands(self, commands):
        self._effector.update_after_actions()

        message = PlayerSendCommands.all_to_str(commands)
        return message

    def parse_init(self, message):
        message = message.split(" ")
        unum = int(message[2])
        side = message[1]

        self._world.init(self._team_name, side, unum, self._goalie)
        if ServerParam.i().is_fullstate(side) and team_config.FULL_STATE_DEBUG:
            self._full_world.init(self._team_name, side, unum, False)
        log.setup(self._team_name, unum, self._current_time)

    def set_view_action(self, view_action: ViewAction):
        self._view_action = view_action

    def set_neck_action(self, neck_action: NeckAction):
        self._neck_action = neck_action

    def set_focus_point_action(self, focus_point_action: FocusPointAction):
        self._focus_point_action = focus_point_action
