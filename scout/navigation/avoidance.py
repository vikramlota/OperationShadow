class ReactiveObstacleAvoidance:
    """
    Deterministic reactive obstacle avoidance for SCOUT ground rover (TRD §15).
    Reads front ToF / ultrasonic distance readings and overrides forward velocity
    if an obstacle is closer than the minimum safety margin.
    """

    def __init__(self, min_distance_cm: float = 30.0) -> None:
        self.min_distance_cm = min_distance_cm

    def evaluate(self, forward_distance_cm: float) -> bool:
        """Returns True if path is clear, False if obstacle detected within margin."""
        return forward_distance_cm >= self.min_distance_cm
