"""
建议模型连通性检测 —— 在跑正式验证前先确认密钥和模型名是否可用。

用法：
    python -m tests.test_llm                    # 检测 config.py 里配置的供应商
    python -m tests.test_llm --provider mimo    # 临时指定供应商
    python -m tests.test_llm --probe            # 逐个试探候选模型名，找出可用的

为什么需要它：各家的「模型标识符」常与控制台显示的名称不同
（例：讯飞界面写 "Spark Max-32K"，API 里要填 "max-32k"），
猜错就是一句 404，看不出是密钥错还是模型名错。本脚本把这两种情况分开。
"""

import argparse
import sys
import time

import providers
from model_catalog import FALLBACK_MODEL_CANDIDATES
from suggest import PROVIDERS

# 保留旧名称，避免已有命令或外部脚本受影响。
CANDIDATES = FALLBACK_MODEL_CANDIDATES


def try_model(engine, model):
    """用最小请求试一个模型名，返回 (是否成功, 信息)"""
    original = engine.model
    engine.model = model
    try:
        t0 = time.time()
        out = engine._call("你是一个助手。", "回复两个字：正常")
        return True, f"{out.strip()[:20]}  ({time.time()-t0:.1f}s)"
    except Exception as e:
        return False, str(e).replace("\n", " ")[:110]
    finally:
        engine.model = original


_BENCH_PROMPT = """客户说："你们这个模块能不能支持自定义审批流？就是简单改一下，
按金额分几档走不同的人。"请站在我方立场，给出 2 条应对话术建议，
每条包含意图提示和建议话术。用 JSON 输出。"""


def bench(engine, models):
    """对比各模型在真实长度请求下的耗时 —— 建议延迟是 P0 指标（PRD < 5s）"""
    print(f"\n用真实长度的建议生成请求测速（每个模型 2 次取均值）\n")
    results = []
    for m in models:
        times = []
        err = None
        for _ in range(2):
            original = engine.model
            engine.model = m
            try:
                t0 = time.time()
                engine._call("你是资深售前顾问的会议助手。", _BENCH_PROMPT)
                times.append(time.time() - t0)
            except Exception as e:
                err = str(e).replace("\n", " ")[:60]
                break
            finally:
                engine.model = original
        if times:
            avg = sum(times) / len(times)
            flag = "✅" if avg < 5 else ("⚠️" if avg < 10 else "❌")
            print(f"   {flag} {m:16s} {avg:5.1f}s")
            results.append((avg, m))
        else:
            print(f"      {m:16s} 不可用 — {err}")
    if results:
        results.sort()
        print(f"\n最快：{results[0][1]}（{results[0][0]:.1f}s）")
        if results[0][0] >= 5:
            print("⚠️ 均未达到 PRD 的 <5s 目标 —— 开发时需用流式输出改善首字延迟，")
            print("   或考虑更换供应商（阿里云 qwen-plus 实测 5.5-7.2s）。")
        else:
            print(f'建议在 config.py 设置：LLM_MODEL = "{results[0][1]}"')


def bench_all(runs=3):
    """跨供应商基准测速 —— 对所有已配置密钥的供应商跑同一请求并排名。

    ⚠️ 只看均值会漏掉真问题：实测 X2-Flash 多数在 8-9s，偶尔飙到 19s。
    会议现场偶尔卡 20 秒同样毁体验，因此同时报告最慢一次（近似 P95）。
    """
    from suggest import PROVIDERS
    print("\n跨供应商基准测速（仅测已配置密钥的供应商）")
    print(f"每家跑 {runs} 次真实长度请求，同时看均值与最慢一次\n")
    print(f"  {'供应商':22s} {'模型':18s} {'均值':>7s} {'最慢':>7s}")
    print("  " + "─" * 58)

    rows = []
    for name in PROVIDERS:
        if name == "custom":
            continue
        try:
            eng = providers.build_llm(kb=None, provider=name)
        except SystemExit:
            continue          # 未配置密钥，静默跳过
        except Exception:
            continue
        times, err = [], None
        for _ in range(runs):
            try:
                t0 = time.time()
                eng._call("你是资深售前顾问的会议助手。", _BENCH_PROMPT)
                times.append(time.time() - t0)
            except Exception as e:
                err = str(e).replace("\n", " ")[:40]
                break
        if not times:
            print(f"  {eng.label:22s} {eng.model:18s}   失败 {err or ''}")
            continue
        avg, mx = sum(times) / len(times), max(times)
        flag = "✅" if mx < 5 else ("⚠️" if mx < 10 else "❌")
        print(f"{flag} {eng.label:22s} {eng.model:18s} {avg:6.1f}s {mx:6.1f}s")
        rows.append((avg, mx, eng.label, eng.model, name))

    if not rows:
        print("\n没有任何已配置密钥的供应商。请先在 config.py 填入至少一家。")
        return
    rows.sort()
    print()
    a, m, label, model, key = rows[0]
    print(f"最快：{label} / {model} —— 均值 {a:.1f}s，最慢 {m:.1f}s")
    print(f'配置方式：LLM_PROVIDER = "{key}"')
    stable = [r for r in rows if r[1] < 5]
    if stable:
        print(f"\n✅ 最慢一次也在 5s 内的有："
              f"{'、'.join(r[2] for r in stable)}")
    else:
        print("\n⚠️ 没有供应商能稳定在 5s 内。开发时须用流式输出改善首字延迟。")


def main():
    ap = argparse.ArgumentParser(description="建议模型连通性检测")
    ap.add_argument("--bench-all", action="store_true",
                    help="跨供应商测速排名（自动跳过未配置密钥的）")
    ap.add_argument("--provider", help="临时指定供应商（覆盖 config.py）")
    ap.add_argument("--probe", action="store_true", help="逐个试探候选模型名")
    ap.add_argument("--bench", action="store_true", help="对比同供应商各模型耗时")
    args = ap.parse_args()

    if args.bench_all:
        bench_all()
        return

    engine = providers.build_llm(kb=None, provider=args.provider)
    src, key = providers.LAST_KEY_SOURCE.get("llm", (None, None))
    print(f"\n供应商：{engine.label}")
    print(f"接口地址：{engine.base_url}")
    print(f"配置的模型：{engine.model}")
    print(f"使用的密钥：{src} = {providers.mask(key)}\n")

    prov = args.provider or providers._cfg("LLM_PROVIDER", default="xfyun")

    # 先试配置的模型
    ok, msg = try_model(engine, engine.model)
    if ok:
        print(f"✅ 连接正常 —— 模型「{engine.model}」可用")
        print(f"   模型回复：{msg}")
        if args.bench:
            cands = [engine.model] + [m for m in CANDIDATES.get(prov, [])
                                      if m != engine.model]
            bench(engine, cands)
        else:
            print(f"\n可以继续跑验证了：")
            print(f"   python run_suggest.py --case 4")
            print(f"若想对比各模型速度：python -m tests.test_llm --bench")
        return

    print(f"❌ 模型「{engine.model}」调用失败：\n   {msg}\n")

    low = msg.lower()

    # 这个错最有价值：模型名是对的，但当前凭证没有该模型的权限
    if "appidnoauth" in low.replace(" ", "") or "noauth" in low:
        print("👉 【模型名正确，但这个 APIPassword 没有该模型的权限】")
        print("   讯飞每个模型的 APIPassword 是独立的。请到控制台找到")
        print("   该模型自己的 APIPassword，填到 config.py 对应变量：")
        print("      星火 X2   → XFYUN_X2_PASSWORD")
        print("      星火 X1.5 → XFYUN_X15_PASSWORD")
        print("      经典系列  → XFYUN_SPARK_PASSWORD")
        print("   （若控制台未开通该模型，需先开通）")
        return

    if "invalid param model" in low:
        print("👉 看起来是【模型名不对】。加 --probe 自动试探：")
        print(f"   python -m tests.test_llm --provider {args.provider or ''} --probe")
        return

    if any(k in low for k in ("401", "403", "auth", "invalid api", "appid",
                              "licc", "密钥", "鉴权")):
        print("👉 看起来是【密钥问题】，请检查：")
        print("   · APIPassword 是否复制完整（讯飞的形如 AK:SK，中间有冒号）")
        print("   · 是否用错了凭证 —— RTASR 的 APIKey 不能用于星火，")
        print("     且各模型的 APIPassword 相互独立")
        print("   · 控制台是否已开通该模型服务")
        return

    cands = [m for m in CANDIDATES.get(prov, []) if m != engine.model]
    if not args.probe:
        print("👉 看起来是【模型名问题】。加 --probe 自动试探可用的模型名：")
        print("   python -m tests.test_llm --probe")
        return

    print(f"开始试探 {len(cands)} 个候选模型名…\n")
    works, no_auth = [], []
    for m in cands:
        ok, info = try_model(engine, m)
        low = info.lower().replace(" ", "")
        if not ok and ("appidnoauth" in low or "noauth" in low):
            no_auth.append(m)
            print(f"   🔑 {m:16s} 模型名有效，但当前密钥无权限")
            continue
        print(f"   {'✅' if ok else '  '} {m:16s} {info if ok else info[:66]}")
        if ok:
            works.append(m)

    print()
    if no_auth and not works:
        print(f"👉 找到有效模型名：{'、'.join(no_auth)}")
        print("   但当前 APIPassword 没有它的权限 —— 讯飞每个模型的密钥独立。")
        print("   请到控制台取【该模型自己的 APIPassword】填入 config.py：")
        print("      星火 X2 → XFYUN_X2_PASSWORD")
        return
    if works:
        print(f"✅ 可用的模型名：{'、'.join(works)}")
        print(f"\n请在 config.py 中设置：")
        print(f'   LLM_MODEL = "{works[0]}"')
    else:
        print("❌ 所有候选都失败了。请到控制台确认已开通的模型及其 API 标识符。")


if __name__ == "__main__":
    main()
