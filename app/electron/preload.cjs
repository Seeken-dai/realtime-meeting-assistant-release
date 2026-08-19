const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("meetingCopilot", {
  runtimeStatus: () => ipcRenderer.invoke("runtime:status"),
  listInputDevices: () => ipcRenderer.invoke("meeting:list-input-devices"),
  testInputDevice: (device) => ipcRenderer.invoke("meeting:test-input-device", device),
  startMeeting: (options) => ipcRenderer.invoke("meeting:start", options),
  warmupMeeting: (options) =>
    ipcRenderer.invoke("meeting:warmup", options || {}),
  beginMeetingRecording: () => ipcRenderer.invoke("meeting:begin-recording"),
  recommendMeetingScene: (input) =>
    ipcRenderer.invoke("meeting:recommend-scene", input || {}),
  setMeetingControls: (controls) =>
    ipcRenderer.invoke("meeting:set-controls", controls),
  stopMeeting: () => ipcRenderer.invoke("meeting:stop"),
  ask: (question) => ipcRenderer.invoke("meeting:ask", question),
  suggestNow: () => ipcRenderer.invoke("meeting:suggest-now"),
  setMeSpeaker: (speakerId) => ipcRenderer.invoke("meeting:set-me", speakerId),
  voiceprintStatus: () => ipcRenderer.invoke("voiceprint:status"),
  enrollVoiceprint: (options) => ipcRenderer.invoke("voiceprint:enroll", options || {}),
  clearVoiceprint: () => ipcRenderer.invoke("voiceprint:clear"),
  removeLastVoiceprintSample: () => ipcRenderer.invoke("voiceprint:remove-last"),
  listMeetingRecords: () => ipcRenderer.invoke("records:list"),
  loadMeetingRecord: (id) => ipcRenderer.invoke("records:load", id),
  saveMeetingRecord: (record) => ipcRenderer.invoke("records:save", record),
  deleteMeetingRecords: (ids) => ipcRenderer.invoke("records:delete", ids),
  saveMeetingDocuments: (meetingId, documents) =>
    ipcRenderer.invoke("records:save-documents", meetingId, documents),
  exportMeetingRecord: (id, format) =>
    ipcRenderer.invoke("records:export", id, format),
  listProjects: () => ipcRenderer.invoke("projects:list"),
  saveProject: (project) => ipcRenderer.invoke("projects:save", project),
  deleteProject: (id) => ipcRenderer.invoke("projects:delete", id),
  listGlossaryTerms: (scope) => ipcRenderer.invoke("glossary:list", scope),
  saveGlossaryTerm: (term) => ipcRenderer.invoke("glossary:save", term),
  deleteGlossaryTerm: (id) => ipcRenderer.invoke("glossary:delete", id),
  listGlossaryTermsForMeeting: (projectId) =>
    ipcRenderer.invoke("glossary:for-meeting", projectId),
  getProjectDocuments: (projectId) =>
    ipcRenderer.invoke("projects:documents:get", projectId),
  setProjectDocuments: (projectId, docIds) =>
    ipcRenderer.invoke("projects:documents:set", projectId, docIds),
  listDocuments: (projectId) => ipcRenderer.invoke("documents:list", projectId),
  pickDocuments: (projectId) => ipcRenderer.invoke("documents:pick", projectId),
  pickDocumentFolder: (projectId) =>
    ipcRenderer.invoke("documents:pick-folder", projectId),
  addDocumentPaths: (filePaths, projectId) =>
    ipcRenderer.invoke("documents:add-paths", filePaths, projectId),
  removeDocument: (id) => ipcRenderer.invoke("documents:remove", id),
  renameDocument: (id, name) => ipcRenderer.invoke("documents:rename", id, name),
  loadMeetingAudio: (id) => ipcRenderer.invoke("records:audio", id),
  diarizeMeeting: (id, opts) => ipcRenderer.invoke("records:diarize", id, opts || {}),
  generateMeetingMinutes: (id, opts) =>
    ipcRenderer.invoke("records:minutes:generate", id, opts || {}),
  generateMeetingReview: (id, opts) =>
    ipcRenderer.invoke("records:review:generate", id, opts || {}),
  saveMeetingMemoryItem: (meetingId, item) =>
    ipcRenderer.invoke("records:memory:save", meetingId, item || {}),
  saveGlossaryCandidates: (meetingId, candidates) =>
    ipcRenderer.invoke("records:glossary-candidates:save", meetingId, candidates || []),
  promoteGlossaryCandidates: (meetingId, candidateIds) =>
    ipcRenderer.invoke("records:glossary-candidates:promote", meetingId, candidateIds || []),
  openFloatingStrategy: () => ipcRenderer.invoke("floating:open"),
  closeFloatingStrategy: () => ipcRenderer.invoke("floating:close"),
  setFloatingStrategyPreferences: (preferences) =>
    ipcRenderer.invoke("floating:set-preferences", preferences || {}),
  dataInfo: () => ipcRenderer.invoke("data:info"),
  revealDataFolder: () => ipcRenderer.invoke("data:reveal"),
  openExternal: (url) => ipcRenderer.invoke("shell:open-external", url),
  clearAllData: () => ipcRenderer.invoke("data:clear"),
  previewDocument: (filePath) => ipcRenderer.invoke("documents:preview", filePath),
  serviceStatus: (opts) => ipcRenderer.invoke("services:status", opts),
  testLlm: (options) => ipcRenderer.invoke("services:test-llm", options),
  probeLlm: (provider) => ipcRenderer.invoke("services:probe-llm", provider),
  testAsr: (opts) => ipcRenderer.invoke("services:test-asr", opts),
  benchmarkProviders: (opts) => ipcRenderer.invoke("services:bench", opts || {}),
  loadState: () => ipcRenderer.invoke("storage:load"),
  saveState: (state) => ipcRenderer.invoke("storage:save", state),
  secretsStatus: () => ipcRenderer.invoke("secrets:status"),
  saveSecrets: (patch) => ipcRenderer.invoke("secrets:save", patch),
  importSecretsFromConfig: () => ipcRenderer.invoke("secrets:import-config"),
  onMeetingEvent: (callback) => {
    const handler = (_event, payload) => callback(payload);
    ipcRenderer.on("meeting:event", handler);
    return () => ipcRenderer.removeListener("meeting:event", handler);
  },
});
