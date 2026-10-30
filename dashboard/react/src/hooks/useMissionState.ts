import { useDashboardStore, type DashboardState } from "../state/store";

/**
 * Mission state selector hook scaffold (Architecture §8.1).
 */
export function useMissionState(): DashboardState {
  const { state } = useDashboardStore();
  return state;
}
