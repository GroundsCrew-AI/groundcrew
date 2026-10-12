"""Emergency actions (docs/phase5-interfaces.md, section 2). Medic finds the implementation
through `get_emergency_actions`."""

from app.actions.base import EmergencyActions
from app.config import Settings
from app.db.session import SessionFactory


def get_emergency_actions(session_factory: SessionFactory, settings: Settings) -> EmergencyActions:
    """The AWS implementation (groundcrew-operate role). It checks each connection's operate
    capability when it plans, so incidents on accounts without it get no emergency action."""
    from app.actions.aws import AwsEmergencyActions

    return AwsEmergencyActions(session_factory, settings)
