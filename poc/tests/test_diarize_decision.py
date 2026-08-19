"""会后簇级认「我」判据的纯逻辑回归；不加载模型和录音。"""

from diarize_offline import choose_me_cluster


def main() -> None:
    cases = [
        # 2026-07-29 15:03 真实会议：人工确认 c0 大部分为用户本人。
        ([0, 1, 2], {0: 0.551, 1: 0.503, 2: 0.432}, (0, "cluster")),
        # 前两簇过近，不应贸然把整簇认成「我」。
        ([0, 1], {0: 0.551, 1: 0.520}, (None, "threshold")),
        # 整体低于声纹可信下限。
        ([0, 1], {0: 0.449, 1: 0.300}, (None, "threshold")),
        # 单簇且分数可信。
        ([4], {4: 0.620}, (4, "cluster")),
    ]
    for ids, scores, expected in cases:
        actual = choose_me_cluster(ids, scores)
        assert actual == expected, (ids, scores, expected, actual)
    print(f"ok: {len(cases)} diarize decision cases")


if __name__ == "__main__":
    main()
