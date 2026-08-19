export type MeetingStartupAction = {
  sendBeginRecording: boolean;
  startClock: boolean;
  cancel: boolean;
};

export function meetingStartupAction(
  stage: string,
  beginRecordingSent: boolean,
): MeetingStartupAction {
  return {
    sendBeginRecording: stage === "ready" && !beginRecordingSent,
    startClock: stage === "listening",
    cancel: stage === "cancelled",
  };
}
