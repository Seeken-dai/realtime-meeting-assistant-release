import type { SuggestionBatch, TranscriptItem } from "./types";

const now = Date.now();

export const demoTranscript: TranscriptItem[] = [
  {
    id: "t1",
    speaker: "客户-张总",
    speakerId: "1",
    text: "我们这边流程比较复杂，跟你们标准的可能不太一样。",
    isFinal: true,
    at: now - 82_000,
  },
  {
    id: "t2",
    speaker: "我",
    speakerId: "2",
    text: "张总，这次主要想跟您确认一下审批这块的需求。",
    isFinal: true,
    at: now - 65_000,
  },
  {
    id: "t3",
    speaker: "客户-张总",
    speakerId: "1",
    text: "能不能支持自定义审批流？就是简单改一下，按金额分几档走不同的人。",
    isFinal: true,
    at: now - 48_000,
  },
  // 以下两段与 t3 同一说话人，用于演示"连说合并、长停顿断行"：
  // t4 紧接着 t3（并入同一行），t5 隔了十几秒（另起一行）。
  // 真实 ASR 的 final 分段就是这么碎，演示模式要如实反映。
  {
    id: "t4",
    speaker: "客户-张总",
    speakerId: "1",
    text: "大概就是这么个意思。",
    isFinal: true,
    at: now - 44_000,
  },
  {
    id: "t5",
    speaker: "客户-张总",
    speakerId: "1",
    text: "对了，还有个事忘了说。",
    isFinal: true,
    at: now - 30_000,
  },
];

export const demoBatch: SuggestionBatch = {
  id: "s1",
  at: now - 43_000,
  elapsed: 4.8,
  hits: [
    {
      source: "产品功能清单.md",
      text: "标准版支持固定审批节点；按条件动态分支需定制开发。",
    },
    {
      source: "需求边界与报价规则.md",
      text: "涉及多条件分支时，报价前需确认全部条件、例外规则与审批角色。",
    },
  ],
  suggestions: [
    {
      level: "grounded",
      grounded: true,
      intent: "“简单改一下”可能掩盖多条件分支，需要先收齐范围。",
      script:
        "张总，按金额走不同审批人属于条件分支，标准版暂不支持，需要定制。您方便把金额区间、对应审批人和例外规则一起列一下吗？",
      references: ["产品功能清单.md", "需求边界与报价规则.md"],
    },
    {
      level: "advisory",
      grounded: false,
      intent: "先确认规则变化频率，避免把可配置需求误做成一次性开发。",
      script:
        "这些金额档位后续会经常调整吗？如果会，建议把规则维护能力也一起纳入范围。",
      references: [],
    },
  ],
};
