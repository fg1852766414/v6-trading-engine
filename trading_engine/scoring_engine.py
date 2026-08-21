"""
ETF综合评分引擎 v3.1 (终极版, 2026-08-01 两边融合)
从主力机构视角，按指标实用性和参考价值打分
总分0-100，映射A/B/C/D/E五档

v3.1 融合记录 (当前项目 + D:\\trading-system 两边统一):
  吸收自副本v3:
    1. 因子分组 + 同组封顶 → 同源信号不叠加(消除双重计分)
    2. 缺失数据不给正分(unknown 默认 0)
    3. EMA确定性平滑(prev_raw 传入, 无全局状态)
    4. 翻转惩罚(评分频繁翻转扣分)
    5. pa_valid 有效性确认(仅有效底分型加分)
    6. RSI只做极端扣分(不褒奖中性区)
  保留自当前项目v3(用户拍板):
    7. [已删除] 份额因子(用户"份额没什么卵用", v3.2移除)
    8. 新增"价格位置"组(距EMA20偏离, 均值回归视角, 独立维度)
    9. 量价组cap=25(用户偏好: 资金信号最不骗人)

v3.4 更新 (2026-08-05):
  恢复100分制 (用户重新分配): 组1趋势(0-50)=道氏25+威科夫20+PA5 / 组2量价(0-35)=CMF10+OBV15+AD10 / 组4位置(0-15)
  扣分上限 -33, 等级阈值回到 A≥80/B≥60/C≥40/D≥20

因子结构 (v3.4):
  组1 趋势结构(0-50) = 道氏(25) + 威科夫(20) + PA确认(5)
  组2 量价确认(0-35) = CMF(10) + OBV(15) + AD(10)
  组4 价格位置(0-15) = 距EMA20偏离(均值回归)
  稳定扣分(-33~0)    = RSI极端(-9) + 数据缺失(-14) + 翻转惩罚(-7)
  同组封顶，跨组不叠加。一次OHLCV波动最多影响一个组的子项。
"""
from typing import Dict, Any, Tuple, List, Optional


def _group_cap(value: int, cap: int) -> int:
    return max(-cap, min(cap, value))


def _ema_smooth(raw_scores: List[int], alpha: float = 0.4) -> int:
    """确定性EMA平滑，只依赖原始分序列，无全局状态"""
    if not raw_scores:
        return 0
    if len(raw_scores) == 1:
        return raw_scores[0]
    result = raw_scores[0]
    for s in raw_scores[1:]:
        result = round(result * (1 - alpha) + s * alpha)
    return result


def _flip_count(scores: List[int]) -> int:
    """最近20分翻转次数"""
    if len(scores) < 3:
        return 0
    cnt = 0
    for i in range(2, min(len(scores), 21)):
        d1 = scores[i-1] - scores[i-2]
        d2 = scores[i] - scores[i-1]
        if d1 != 0 and d2 != 0 and d1 * d2 < 0:
            cnt += 1
    return cnt


def score_etf(
    r: Dict[str, Any],
    prev_raw: Optional[List[int]] = None,
) -> Tuple[int, str, str, str, bool]:
    """
    对单只ETF打分。
    输入:
        r: row_data（包含所有引擎输出）
        prev_raw: 历史原始评分列表(最近优先)，用于确定性平滑
    输出: (总分, 等级, 等级标签, 详情摘要, e0_warning)
    """
    details = []
    total = 0

    # ── 数据完整性检查 ──
    # 威科夫阶段未知/缺失时不再给正分
    w_letter = r.get('w_letter', '')
    if not w_letter or w_letter == '?':
        w_letter = 'unknown'

    # ── 组1: 趋势结构 (0-50) (v3.4: 100分制, 道氏25+威科夫20+PA5) ──
    # 道氏方向
    p_dir = r.get('dow_p_dir', '')
    s_dir = r.get('dow_s_dir', '')
    trend_base = 15 if p_dir == 'bull' else 8 if p_dir == 'bear' else 12
    if p_dir == s_dir and p_dir != '':
        if p_dir == 'bull':
            trend_base += 10  # 双牛确认 -> 强势(满分25)
        elif p_dir == 'bear':
            trend_base -= 8  # 双熊确认 -> 弱势
    elif p_dir == 'bear' and s_dir == 'bull':
        trend_base += 3  # 主空次多 -> 可能反弹

    # 威科夫阶段
    w_scores = {
        'B': 15, 'A→B': 12, 'A': 10, 'C→A': 8, 'C': 2, 'D': 0,
    }
    w_part = w_scores.get(w_letter, 0)  # unknown/缺失 -> 0分

    # 威科夫子信号：不超过±5
    w_bonus = 0
    if r.get('w_sos_det'): w_bonus += 3
    if r.get('w_lps_det'): w_bonus += 2
    if r.get('w_spring_det'): w_bonus += 1
    if r.get('w_st'): w_bonus += 1
    if r.get('w_ut'): w_bonus -= 4
    w_bonus = _group_cap(w_bonus, 5)
    w_part += w_bonus

    # PA分型确认（仅限额外5分，不单独成组）
    pk = r.get('pk', '')
    pa_part = 0
    pa_valid = r.get('pa_valid', False)  # 底分型是否有效（EMA压制下常无效，不加分）
    if pk == 'bottom' and pa_valid and w_letter in ('B', 'A→B', 'A', 'C→A'):
        pa_part = 5  # 仅有效底分型对趋势有确认作用（含C→A过渡期）
    elif pk == 'bottom' and not pa_valid:
        pa_part = 0  # 无效底分型：EMA压制/下跌中，不给正分
    elif pk == 'top' and w_letter in ('C', 'D'):
        pa_part = -3  # 顶分型加强空头信号

    group1 = _group_cap(trend_base + w_part + pa_part, 50)
    total += group1
    details.append(f"趋势{group1}(道{trend_base}+威{w_part}+PA{pa_part})")

    # ── 组2: 量价确认 (0-35) (v3.4: 100分制, CMF10+OBV15+AD10) ──
    # CMF
    cmf_raw = r.get('vol_cmf', '')
    cmf_val = 0.0
    if cmf_raw:
        try:
            cmf_val = float(cmf_raw.split('|')[0])
        except:
            pass
    if cmf_val > 0.2:
        cmf_part = 10
    elif cmf_val > 0.05:
        cmf_part = 7
    elif cmf_val > -0.05:
        cmf_part = 4
    elif cmf_val > -0.2:
        cmf_part = -5
    else:
        cmf_part = -7

    # OBV方向和背离（单因子）
    obv = r.get('vol_obv', '')
    obv_div = r.get('vol_obv_div', 'none')
    if obv == 'bullish' and obv_div == 'bullish_div':
        obv_part = 15
    elif obv == 'bullish':
        obv_part = 10
    elif obv_div == 'bullish_div':
        obv_part = 6
    elif obv == 'bearish' and obv_div == 'bearish_div':
        obv_part = -10
    elif obv == 'bearish':
        obv_part = -6
    else:
        obv_part = 0

    # AD方向
    ad = r.get('vol_ad_t', '')
    ad_part = 10 if ad == 'bullish' else (-8 if ad == 'bearish' else 0)

    group2 = _group_cap(cmf_part + obv_part + ad_part, 35)
    total += group2
    details.append(f"量价{group2}(C{cmf_part}+O{obv_part}+A{ad_part})")

    # ── 组3: 已删除 (v3.3: 份额+溢价因子全部移除, 用户"没什么卵用") ──

    # ── 组4: 价格位置 (0-10) (v3.1新增: 距EMA20偏离, 均值回归视角) ──
    dist = r.get('dist_v', 0) or 0
    try:
        dist = float(dist)
    except (ValueError, TypeError):
        dist = 0.0
    if 1.0 <= dist <= 3.0:
        pos_part = 15    # 站上均线但未过度偏离 = 最健康蓄势
    elif 0 < dist < 1.0 or 3.0 < dist <= 6.0:
        pos_part = 10
    elif -2.0 <= dist < 0:
        pos_part = 6     # 刚回踩均线
    elif 6.0 < dist <= 10.0:
        pos_part = 4     # 涨太猛,乖离过大
    elif -5.0 <= dist < -2.0:
        pos_part = 2
    else:
        pos_part = -6    # 深跌破位(< -5%) / 严重超买(> +10%)

    group4 = _group_cap(pos_part, 15)
    total += group4
    details.append(f"位置{group4}")

    # ── 稳定扣分 (-33~0) (v3.4: 100分制按比例放大) ──
    penalty = 0

    # RSI极端
    rsi = r.get('rsi_v', None)
    if rsi is not None:
        if rsi < 20 or rsi > 80:
            penalty -= 9
        elif rsi < 25 or rsi > 75:
            penalty -= 5
        elif rsi < 30 or rsi > 70:
            penalty -= 1

    # 数据缺失惩罚
    missing_count = 0
    if not w_letter or w_letter == 'unknown': missing_count += 1
    if not p_dir: missing_count += 1
    if not cmf_raw: missing_count += 1
    if missing_count >= 2:
        penalty -= 7  # 2个以上字段缺失 -> 惩罚
    if missing_count >= 4:
        penalty -= 7  # 严重缺失

    # 翻转惩罚(从prev_raw计算)
    if prev_raw and len(prev_raw) >= 3:
        flip = _flip_count(prev_raw)
        if flip >= 5:
            penalty -= 7
        elif flip >= 3:
            penalty -= 4

    penalty = max(-33, penalty)
    total += penalty
    details.append(f"扣分{penalty}")

    # ── 最终修正 ──
    total = max(0, min(100, total))  # v3.4: 恢复100分制

    # 等级判定 (v3.4: 100分制阈值 A≥80/B≥60/C≥40/D≥20)
    if total >= 80:
        grade = 'A'
        grade_label = '优质'
    elif total >= 60:
        grade = 'B'
        grade_label = '良好'
    elif total >= 40:
        grade = 'C'
        grade_label = '一般'
    elif total >= 20:
        grade = 'D'
        grade_label = '差'
    else:
        grade = 'E'
        grade_label = '极差'

    e0_warning = total == 0

    # 稳定分（确定性EMA，从prev_raw计算）
    prev_scores = prev_raw or []
    stable_raw = prev_scores + [total]
    stable = _ema_smooth(stable_raw, alpha=0.4)
    stable_grade = 'A' if stable >= 80 else 'B' if stable >= 60 else 'C' if stable >= 40 else 'D' if stable >= 20 else 'E'

    daily_change = total - (prev_scores[-1] if prev_scores else total)
    flip_cnt = _flip_count(prev_scores + [total])

    return (
        total, grade, grade_label, ' | '.join(details), e0_warning,
        stable, stable_grade, daily_change, flip_cnt,
    )
