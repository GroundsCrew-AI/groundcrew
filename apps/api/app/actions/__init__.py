"""Emergency actions (docs/phase5-interfaces.md, section 2). Medic finds the implementation
through `get_emergency_actions`."""

from app.actions.base import EmergencyActions
from app.config import Settings
from app.db.session import SessionFactory


def get_emergency_actions(session_factory: SessionFactory, settings: Settings) -> EmergencyActions:
    """AWS (groundcrew-operate role), Kubernetes (the cluster's kubeconfig), and Groundcrew
    agents (signed commands) behind one router. Each checks, when it plans, that the connection
    may act (operate capability; for an agent, the service's `allow`), so incidents elsewhere
    get no emergency action."""
    from app.actions.agent import AgentEmergencyActions
    from app.actions.aws import AwsEmergencyActions
    from app.actions.base import EmergencyActionRouter
    from app.actions.k8s import K8sEmergencyActions

    return EmergencyActionRouter(
        AwsEmergencyActions(session_factory, settings),
        K8sEmergencyActions(session_factory, settings),
        AgentEmergencyActions(session_factory, settings),
    )
