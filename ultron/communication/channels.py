class CommunicationChannels:
    """Fixed communication topic/channel strings defined in TRD v2.0 §20."""
    SCOUT_COMMAND = "scout/01/command"
    SCOUT_STATUS = "scout/01/status"
    SCOUT_OBSERVATION = "scout/01/observation"
    SCOUT_SENSOR = "scout/01/sensor"

    HAWK_COMMAND = "hawk/01/command"
    HAWK_STATUS = "hawk/01/status"
    HAWK_OBSERVATION = "hawk/01/observation"
    HAWK_TELEMETRY = "hawk/01/telemetry"

    ULTRON_MISSION = "ultron/mission"
    ULTRON_REPLAN = "ultron/replan"
