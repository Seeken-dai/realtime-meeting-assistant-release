const SCENE_LABELS = {
  general: "通用会议",
  sales: "售前沟通",
  requirements: "需求评审",
};

const SCENE_KEYWORDS = {
  sales: ["售前", "客户", "报价", "商务", "方案", "招投标", "采购", "销售", "预算", "交付"],
  requirements: ["需求", "评审", "澄清", "规格", "接口", "数据", "验收", "业务规则", "异常", "范围", "prd", "原型"],
};

const LEGACY_PLACEHOLDER_TITLES = new Set([
  "xx项目需求澄清会",
  "xx 项目需求澄清会",
  "未命名会议",
]);

function meaningfulTitle(value) {
  const title = String(value || "").trim();
  return LEGACY_PLACEHOLDER_TITLES.has(title.toLowerCase()) ? "" : title;
}

function recommendMeetingScene(input = {}) {
  const title = meaningfulTitle(input.title);
  const sources = [
    { text: title, weight: 3 },
    { text: input.projectName, weight: 2 },
    ...(Array.isArray(input.documentNames)
      ? input.documentNames.map((text) => ({ text, weight: 1 }))
      : []),
  ].filter((source) => String(source.text || "").trim());
  const scores = Object.fromEntries(
    Object.keys(SCENE_KEYWORDS).map((scene) => [scene, 0]),
  );
  const hits = Object.fromEntries(
    Object.keys(SCENE_KEYWORDS).map((scene) => [scene, new Set()]),
  );
  for (const source of sources) {
    const corpus = String(source.text).toLowerCase();
    for (const [scene, keywords] of Object.entries(SCENE_KEYWORDS)) {
      for (const keyword of keywords) {
        if (!corpus.includes(keyword.toLowerCase())) continue;
        scores[scene] += source.weight;
        hits[scene].add(keyword);
      }
    }
  }
  const ranked = Object.entries(scores).sort((a, b) => b[1] - a[1]);
  const winner = ranked[0];
  const runnerUp = ranked[1];
  if (!winner || winner[1] === 0) {
    return {
      scene: "general",
      label: SCENE_LABELS.general,
      reason: "本场标题、项目和已选资料没有明显的业务信号，先使用通用会议。",
      confidence: "low",
    };
  }
  if (runnerUp && runnerUp[1] === winner[1]) {
    return {
      scene: "general",
      label: SCENE_LABELS.general,
      reason: "售前与需求信号接近，先保持通用会议，请按本场目标手动选择。",
      confidence: "low",
    };
  }
  if (winner[1] < 2) {
    return {
      scene: "general",
      label: SCENE_LABELS.general,
      reason: "只命中一个较弱的资料名关键词，先保持通用会议。",
      confidence: "low",
    };
  }
  const scene = winner[0];
  const matched = Array.from(hits[scene]).slice(0, 4).join("、");
  const reason = scene === "requirements"
    ? `命中了${matched || "需求/验收"}等关键词，适合追踪边界和验收标准。`
    : `命中了${matched || "客户/商务"}等关键词，适合关注客户目标和承诺边界。`;
  return {
    scene,
    label: SCENE_LABELS[scene],
    reason,
    confidence: winner[1] >= 5 ? "high" : winner[1] >= 2 ? "medium" : "low",
  };
}

module.exports = {
  meaningfulTitle,
  recommendMeetingScene,
};
