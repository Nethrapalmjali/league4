"""Synchronization between EMS Auction (Player & TeamOwner) and Scorecard (ScTeam & ScPlayer)."""

import logging
from .ems import db
from .models import ScEvent, ScPlayer, ScTeam

logger = logging.getLogger(__name__)


def sync_sold_player(player, owner):
    """Synchronize an auctioned player into the scorecard team squad.

    Finds or creates the corresponding ScTeam for the franchise and sport,
    then adds or activates the ScPlayer for that team.
    """
    if not player or not owner or not player.sold:
        return None

    try:
        gender = "Women" if getattr(player, "gender", None) == "Female" else "Men"
        sport = player.sport

        # 1. Match existing ScTeam by ems_owner_id or name and sport
        sc_team = ScTeam.query.filter(
            ScTeam.sport == sport,
            db.or_(
                ScTeam.ems_owner_id == owner.id,
                db.func.lower(ScTeam.name) == owner.team_name.lower().strip(),
            ),
        ).first()

        if not sc_team:
            sc_team = ScTeam(
                name=owner.team_name.strip(),
                short_name=(owner.team_name.strip()[:4]).upper(),
                sport=sport,
                gender=gender,
                ems_owner_id=owner.id,
                logo=owner.team_logo or owner.team_owner_photo,
            )
            db.session.add(sc_team)
            db.session.flush()
        else:
            if not sc_team.ems_owner_id:
                sc_team.ems_owner_id = owner.id
            if not sc_team.logo and (owner.team_logo or owner.team_owner_photo):
                sc_team.logo = owner.team_logo or owner.team_owner_photo

        # 2. Match ScPlayer by ems_player_id or name in that team
        sc_player = ScPlayer.query.filter(
            db.or_(
                ScPlayer.ems_player_id == player.id,
                db.and_(
                    ScPlayer.team_id == sc_team.id,
                    db.func.lower(ScPlayer.name) == player.name.lower().strip(),
                ),
            )
        ).first()

        if not sc_player:
            sc_player = ScPlayer(
                team_id=sc_team.id,
                name=player.name.strip(),
                jersey_no=None,
                ems_player_id=player.id,
                active=True,
            )
            db.session.add(sc_player)
        else:
            sc_player.team_id = sc_team.id
            sc_player.ems_player_id = player.id
            sc_player.active = True

        db.session.commit()
        return sc_player
    except Exception as e:
        db.session.rollback()
        logger.warning(f"Failed to sync sold player {getattr(player, 'id', None)} to scorecard: {e}")
        return None


def sync_reopened_player(player):
    """Remove or deactivate an un-auctioned/reopened player from scorecard squads."""
    if not player:
        return False
    try:
        sc_players = ScPlayer.query.filter_by(ems_player_id=player.id).all()
        for sc_p in sc_players:
            has_events = ScEvent.query.filter_by(player_id=sc_p.id).count()
            if has_events:
                sc_p.active = False
            else:
                db.session.delete(sc_p)
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        logger.warning(f"Failed to remove reopened player {getattr(player, 'id', None)} from scorecard: {e}")
        return False


def sync_all_auction_squads():
    """Batch-sync all currently sold players across all sports into their scorecard teams."""
    from app import Player, TeamOwner

    sold_players = Player.query.filter_by(sold=True).all()
    owners = TeamOwner.query.all()
    owners_by_name = {o.team_name.lower().strip(): o for o in owners}
    synced_count = 0

    for p in sold_players:
        if not p.sold_to:
            continue
        owner = owners_by_name.get(p.sold_to.lower().strip())
        if owner:
            res = sync_sold_player(p, owner)
            if res:
                synced_count += 1

    return synced_count
