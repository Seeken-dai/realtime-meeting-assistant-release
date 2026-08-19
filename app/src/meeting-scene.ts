import type { SceneRecommendation } from "./types";

export function shouldAutoApplySceneRecommendation(
  recommendation: SceneRecommendation | null,
  selectionTouched: boolean,
): boolean {
  return Boolean(
    recommendation &&
      recommendation.confidence !== "low" &&
      !selectionTouched,
  );
}
