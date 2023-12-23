from __future__ import annotations
from math import log10
from typing import TYPE_CHECKING, Any
from datetime import datetime
from pony.orm import PrimaryKey, Set, Required, db_session

from common.database import db
from common.exceptions import (
    CannotTransferToBannedError,
    CannotTransferToBotError,
    CannotTransferToSelfError,
    CannotTransferToUnknownPlayerError,
    NotEnoughNkapError,
    UserIsBanned,
)
from config import BASE_POINTS, BOT_ID

if TYPE_CHECKING:
    from game import Game
    from telegram import User


class UserDB(db.Entity):
    """The user entity"""

    _table_ = "users"
    id = PrimaryKey(int, auto=False, size=64)  # Telegram User ID
    name = Required(str)
    verified = Required(bool, default=False)
    # Relationship to Game Statistics
    game_statistics = Set("GameStatisticsDB")
    # Relationship to User Achievements
    achievements = Set("AchievementsDB")


class GameStatisticsDB(db.Entity):
    """Game statistics entity, contains all player stats"""

    _table_ = "stats"
    user = PrimaryKey(UserDB)
    points = Required(int, default=0)
    games_played = Required(int, default=0)
    kicked = Required(int, default=0)
    wrong_card = Required(int, default=0)
    quit = Required(int, default=0)
    slept = Required(int, default=0)
    losses = Required(int, default=0)
    afk = Required(int, default=0)
    losses_special = Required(int, default=0)
    losses_kora = Required(int, default=0)
    losses_dbl_kora = Required(int, default=0)
    wins = Required(int, default=0)
    wins_special = Required(int, default=0)
    wins_kora = Required(int, default=0)
    wins_dbl_kora = Required(int, default=0)
    wl_streak = Required(int, default=0)
    nkap = Required(int, default=30000)


class AchievementsDB(db.Entity):
    """User achievements entity, contains all player achievements"""

    _table_ = "achievements"
    user = Required(UserDB)
    code = Required(str)
    displayed = Required(bool, default=True)
    date_achieved = Required(datetime, default=datetime.now())
    PrimaryKey(user, code)


# ----------------------
# ---- User Model ----
# ----------------------
@db_session
def add_user(user: User) -> None:
    """Adds a user to the database if they don't exist"""
    if not UserDB.exists(id=user.id):
        new_user = UserDB(id=user.id, name=user.first_name, verified=True)
        GameStatisticsDB(user=new_user)
        AchievementsDB(user=new_user, code="ACH_NEW_PLAYER")


@db_session
def get_user(
    user: User,
) -> tuple[UserDB, GameStatisticsDB, Any]:
    """Returns a user and their stats from the database"""
    userdb = UserDB.get(id=user.id)

    # incase the user doesn't exist in the database yet,
    # probably their first time to play
    if not userdb:
        # log: f"[User] {user.id} - {user.first_name} first time user"
        raise ValueError("User doesn't exist in the database")

    if userdb.verified is False:
        raise UserIsBanned()

    gamestatsdb = GameStatisticsDB.get(user=userdb)
    achievements_query = AchievementsDB.select(lambda a: a.user == userdb)

    # players seem to always change name, so we need to update it
    userdb.name = user.first_name

    return (userdb, gamestatsdb, list(achievements_query))


# ----------------------
# ---- Stats Model ----
# ----------------------
@db_session
def get_stats(user: User) -> tuple[UserDB, GameStatisticsDB]:
    """Returns a user's stats from the database"""
    userdb = UserDB.get(id=user.id)
    return userdb, GameStatisticsDB.get(user=userdb)


@db_session
def compute_game_stats(game: Game):
    """Compute the points for the winners and the loosers"""
    winners = game.winners
    loosers = game.losers
    nkap = game.nkap
    points = BASE_POINTS

    # do not compute stats if there's no winner or no looser
    if len(winners) == 0 or len(loosers) == 0:
        return

    # if player wins by kora, double the points and the money
    if game.end_reason == "KORA":
        points *= 2
        nkap *= 2

    # if player wins by double kora, quadruple
    if game.end_reason == "DBL_KORA":
        points *= 4
        nkap *= 4

    # if we are playing a money game, assign points based on the nkap
    if nkap > 0:
        # logarithmic scale for money games
        # details: https://chat.openai.com/share/5db4c576-8e26-4339-a6e5-2e660a492ff6
        points = int(BASE_POINTS * log10(1 + nkap))

    for player in winners:
        # add points to the db
        stats = GameStatisticsDB.get(user=player.user.id)
        stats.points += points
        stats.wins += 1
        stats.games_played += 1
        if stats.wl_streak < 0:
            stats.wl_streak = 1
        else:
            stats.wl_streak += 1

        # if a game finishes by AFK or QUIT at the >3 round, the player wins 3 times the nkap
        # this is because the looser might quit to avoid losing money
        if (game.end_reason == "AFK" or game.end_reason == "QUIT") and game.round >= 3:
            nkap += nkap * 3

        if game.end_reason == "KORA":
            stats.wins_kora += 1
            nkap += nkap * 2
        if game.end_reason == "DBL_KORA":
            stats.wins_dbl_kora += 1
            nkap += nkap * 4
        if game.end_reason == "SPECIAL":
            stats.wins_special += 1

        stats.nkap += nkap

    for player in loosers:
        stats = GameStatisticsDB.get(user=player.user.id)
        stats.points -= points // 2
        stats.losses += 0
        stats.games_played += 1
        if stats.wl_streak > 0:
            stats.wl_streak = -1
        else:
            stats.wl_streak -= 1

        # if a game finishes by AFK or QUIT >3 round,
        # the player loses 3 times the nkap to all players
        if (game.end_reason == "AFK" or game.end_reason == "QUIT") and game.round >= 3:
            nkap -= nkap * 3 * len(winners)

        if game.end_reason == "AFK":
            stats.afk += 1
        if game.end_reason == "QUIT":
            stats.quit += 1
        if game.end_reason == "KORA":
            stats.losses_kora += 1
            nkap -= nkap * 2
        if game.end_reason == "DBL_KORA":
            stats.losses_dbl_kora += 1
            nkap -= nkap * 4
        if game.end_reason == "SPECIAL":
            stats.losses_special += 1

        stats.nkap -= nkap


@db_session
def compute_transfer_nkap(from_id: int, to_id: int, amount: int):
    """Transfer nkap from one user to another"""
    userdb_from = UserDB.get(id=from_id)
    userdb_to = UserDB.get(id=to_id)

    if userdb_to is None:
        raise CannotTransferToUnknownPlayerError()

    stats_from = GameStatisticsDB.get(user=userdb_from)
    stats_to = GameStatisticsDB.get(user=userdb_to)

    if userdb_to.verified is False or userdb_from.verified is False:
        raise CannotTransferToBannedError()

    if stats_from.nkap >= amount:
        stats_from.nkap -= amount
        stats_to.nkap += amount
    else:
        raise NotEnoughNkapError()


@db_session
def compute_ret_rem(user_id: int, amount: int, ret: bool = True):
    """Rem or Ret nkap from one user to another"""
    userdb = UserDB.get(id=user_id)
    stats = GameStatisticsDB.get(user=userdb)

    if userdb.id == BOT_ID:
        raise CannotTransferToBotError()

    if ret:  # retour
        stats.nkap -= amount
    else:  # remboursement
        stats.nkap += amount


@db_session
def compute_ban_unban(user_id: int | User, ban: bool = True):
    """Ban or Unban a user"""
    if not isinstance(user_id, int):
        user_id = user_id.id

    userdb = UserDB.get(id=user_id)

    if userdb.id == BOT_ID:
        raise CannotTransferToBotError()

    if ban:  # ban
        userdb.verified = False
    else:  # unban
        userdb.verified = True


# ----------------------
# ---- Achievements Model -
# ----------------------
@db_session
def get_achievements(user_id: int):
    """Get the achievements of a user"""
    achievements = AchievementsDB.select(lambda a: a.user == user_id)
    return achievements