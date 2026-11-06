class ReactiveObstacleAvoidance:
    """
    Deterministic reactive obstacle avoidance for SCOUT ground rover (TRD §15).
    Reads front ToF / ultrasonic / LiDAR / depth-camera distance readings, triggers
    an immediate stop and reverse maneuver when an obstacle is within the safety margin,
    and signals route change / replanning.
    """

    def __init__(self, min_distance_cm: float = 30.0, reverse_distance_cm: float = 45.0) -> None:
        self.min_distance_cm = min_distance_cm
        self.reverse_distance_cm = max(min_distance_cm, reverse_distance_cm)

    def evaluate(self, forward_distance_cm: float) -> bool:
        """Returns True if path is clear, False if obstacle detected within margin."""
        return forward_distance_cm >= self.min_distance_cm

    def should_reverse(self, forward_distance_cm: float) -> bool:
        """Returns True when the rover must reverse ('S') to create clearance before turning."""
        return 0.0 < forward_distance_cm < self.reverse_distance_cm
