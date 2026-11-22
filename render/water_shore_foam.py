"""岸边泡沫渲染修正后的纯函数实现。

所有函数均为无副作用纯函数，便于 CPU 侧单元测试，同一套公式可直接
移植到 HLSL/GLSL 着色器。管线顺序：

    潮汐修正水深 ─┐
                 ├─ 泡沫遮罩 ─ 纹理密度调制 ─ 边缘淡出 ─ 相交软化 ─ 最终泡沫
    浪高/流速驱动 ┘
    渲染状态：深度写入关闭、主/反射相机共享评估、距离 LOD、夜间衰减。

修复的十个问题：
1. 随潮汐更新（tide_update / shore_depth）
2. UV 按浪向（uv_wave_dir）
3. 浪高驱动（wave_height_drive / flow_speed）
4. 边缘淡出（edge_fade）
5. 不写深度（depth_write_off）
6. 相机一致（camera_consistent / reflect_order）
7. 贴合水面（surface_fit）
8. 夜间衰减（night_atten）
9. 距离降级（distance_lod）
10. 相交处理（intersect_fix）
"""

import math

# ---- 调参常量（与着色器 cbuffer 中的阈值一一对应）----
FOAM_WAVE_MIN = 0.3      # 开始产生碎浪白沫的最小浪高 (m)
FOAM_WAVE_MAX = 1.5      # 浪高驱动达到满幅的浪高 (m)
FOAM_SPEED_MIN = 0.5     # 起沫流速下限 (m/s)
FOAM_SPEED_MAX = 3.0     # 满幅流速 (m/s)
EDGE_FADE_WIDTH = 0.25   # 泡沫边缘淡出带宽度（归一化距离）
NIGHT_DIM_MAX = 0.8      # 深夜最多压掉的自发光比例
LOD_RANGE_MULT = 4.0     # 完全降级距离 = LOD 起始距离 × 4
INTERSECT_BIAS = 0.02    # 礁石相交深度淡化余量 (m)，防 Z-fighting
SURFACE_BIAS = 0.01      # 粒子相对水面的微小抬升 (m)，防 Z-fighting

__all__ = [
    "tide_update",
    "uv_wave_dir",
    "wave_height_drive",
    "edge_fade",
    "depth_write_off",
    "camera_consistent",
    "surface_fit",
    "night_atten",
    "distance_lod",
    "intersect_fix",
    "foam_mask",
    "foam_density",
    "shore_depth",
    "flow_speed",
    "reflect_order",
]


def clamp(x, lo=0.0, hi=1.0):
    """夹取到 [lo, hi]。"""
    return max(lo, min(hi, x))


def smoothstep(edge0, edge1, x):
    """HLSL/GLSL 同名函数：edge0~edge1 之间平滑 0→1。"""
    if edge1 == edge0:
        return 1.0 if x >= edge1 else 0.0
    t = clamp((x - edge0) / (edge1 - edge0))
    return t * t * (3.0 - 2.0 * t)


# ---------------------------------------------------------------------------
# 1. 随潮汐更新
# ---------------------------------------------------------------------------

def shore_depth(water_depth, tide_offset):
    """潮汐修正后的当前水深（相对实时水面，而非固定基准面）。

    water_depth 为基准面到海床的实测水深；tide_offset 为当前潮位
    （涨潮为正）。涨潮时同一海床上方的有效水深同步增加，泡沫带
    依据该有效水深判定，而非写死的水深阈值。
    """
    return max(0.0, water_depth + tide_offset)


def tide_update(tide_level, depth_threshold):
    """随潮汐移动泡沫生成阈值。

    海床点在基准面下 d 处，当前有效水深为 d + tide_level，起沫条件
    d + tide_level <= depth_threshold 等价于
    d <= depth_threshold - tide_level，即基准面坐标下的起沫阈值随潮位
    下移。涨潮时泡沫带整体向岸推进，岸线不再与固定阈值错位。

    返回修正后的基准面坐标起沫阈值。
    """
    return max(0.0, depth_threshold - tide_level)


# ---------------------------------------------------------------------------
# 2. UV 按浪向
# ---------------------------------------------------------------------------

def uv_wave_dir(wave_dir_x, wave_dir_y):
    """按浪向构造旋转正交 UV 基，替代世界坐标平面平铺。

    返回行主序 2×2 矩阵 (c, s, -s, c)：世界平面位移 p 乘该矩阵得到
    浪向对齐的 UV —— u 沿浪向（纹理随波浪流动），v 垂直浪向（纹理
    沿波峰线重复），浪尖处不再被拉成条纹。零向量退化为单位矩阵。
    """
    length = math.hypot(wave_dir_x, wave_dir_y)
    if length <= 1e-8:
        return (1.0, 0.0, 0.0, 1.0)
    c = wave_dir_x / length
    s = wave_dir_y / length
    return (c, s, -s, c)


# ---------------------------------------------------------------------------
# 3. 浪高驱动
# ---------------------------------------------------------------------------

def flow_speed(velocity_x, velocity_y):
    """由平面流速分量合成流速大小，作为碎浪起沫的驱动因子之一。"""
    return math.hypot(velocity_x, velocity_y)


def wave_height_drive(wave_height, speed):
    """浪高 + 流速双因子驱动白沫（碎浪尖 whitecap）。

    两个因子各自经 smoothstep  ramp 后加权求和：浪高占 60%、流速占
    40%。静水面（浪高与流速均为 0）输出恒为 0，不再凭空漂一层泡沫。
    """
    height_factor = smoothstep(FOAM_WAVE_MIN, FOAM_WAVE_MAX, wave_height)
    speed_factor = smoothstep(FOAM_SPEED_MIN, FOAM_SPEED_MAX, speed)
    return clamp(0.6 * height_factor + 0.4 * speed_factor)


# ---------------------------------------------------------------------------
# 遮罩与密度
# ---------------------------------------------------------------------------

def foam_mask(shore_factor, wave_factor):
    """合并岸线冲刷因子与碎浪因子为总泡沫遮罩。

    采用概率并集 a + b - a·b（等价于 1-(1-a)(1-b)），任一因子成立
    即有泡沫，同时避免两因子重复叠加导致超过 1。
    """
    a = clamp(shore_factor)
    b = clamp(wave_factor)
    return clamp(a + b - a * b)


def foam_density(mask, noise):
    """遮罩经泡沫纹理采样调制后的实际密度。

    mask 为 foam_mask 的输出，noise ∈ [0,1] 为程序化/贴图噪声，
    决定泡沫在遮罩区内的斑驳厚薄。
    """
    return clamp(mask) * clamp(noise)


# ---------------------------------------------------------------------------
# 4. 边缘淡出
# ---------------------------------------------------------------------------

def edge_fade(foam_factor, edge_distance):
    """泡沫带边缘 smoothstep 淡出，取代与清水之间的硬切。

    edge_distance 为距泡沫边界的归一化距离：0 表示恰在边界上
    （完全淡出），>= EDGE_FADE_WIDTH 进入带内（全强度）。
    """
    fade_weight = smoothstep(0.0, EDGE_FADE_WIDTH, edge_distance)
    return clamp(foam_factor) * fade_weight


# ---------------------------------------------------------------------------
# 5. 不写深度
# ---------------------------------------------------------------------------

def depth_write_off(depth_write_flag, render_pass):
    """泡沫渲染状态修正：关闭深度写入（ZWrite Off）。

    泡沫是半透明表层效果，只做颜色混合、保留深度测试，避免把深度
    写进缓冲后挡掉其后绘制的水面反射。返回修正后的渲染状态字典。
    """
    return {
        "render_pass": render_pass,
        "depth_write": False,
        "depth_test": True,
        "blend": "alpha",
        "original_depth_write": bool(depth_write_flag),
    }


# ---------------------------------------------------------------------------
# 6. 相机一致
# ---------------------------------------------------------------------------

def camera_consistent(main_camera, reflection_camera):
    """主相机与反射相机共享同一套世界空间泡沫评估。

    两台相机不再各自在视图空间重算泡沫，而是读取同一世界空间泡沫
    缓冲（同一随机种子/同一坐标采样），倒影与实景的泡沫位置偏移
    恒为 0，消除原先半米级的错位。
    """
    return {
        "main_camera": main_camera,
        "reflection_camera": reflection_camera,
        "shared_foam_buffer": True,
        "evaluation_space": "world",
        "position_offset": 0.0,
    }


def reflect_order(main_pass, reflection_pass):
    """渲染次序修正：反射通道先跑，主通道后合成。

    反射相机先渲染平面反射目标（复用共享的世界空间泡沫评估），
    主相机随后绘制水面并合成反射纹理，保证倒影里的泡沫与实景一致。
    """
    return {
        "order": (reflection_pass, main_pass),
        "shared_foam_evaluation": True,
        "composite_pass": main_pass,
    }


# ---------------------------------------------------------------------------
# 7. 贴合水面
# ---------------------------------------------------------------------------

def surface_fit(base_water_height, wave_displacement):
    """泡沫粒子贴合真实起伏水面。

    实时水面高度 = 基准水位 + 波浪位移（浪谷处位移为负），粒子 y
    取该高度再加 SURFACE_BIAS 微抬防 Z-fighting。浪谷处粒子随水面
    下沉，不再浮在空中。返回 (水面高度, 粒子高度)。
    """
    surface_y = base_water_height + wave_displacement
    particle_y = surface_y + SURFACE_BIAS
    return (surface_y, particle_y)


# ---------------------------------------------------------------------------
# 8. 夜间衰减
# ---------------------------------------------------------------------------

def night_atten(light_level, night_factor):
    """夜间泡沫自发光衰减，避免亮成一条灯带。

    light_level 为当前环境/主光强度（1 为正午），night_factor ∈ [0,1]
    为夜色程度。返回亮度/自发光乘数：深夜最多压暗 NIGHT_DIM_MAX，
    泡沫只保留微弱反照而不自发光。
    """
    night = clamp(night_factor)
    return clamp(light_level) * (1.0 - NIGHT_DIM_MAX * night)


# ---------------------------------------------------------------------------
# 9. 距离降级
# ---------------------------------------------------------------------------

def distance_lod(distance, lod_start):
    """按距离逐级降级泡沫细节，消除远处高频噪点闪烁。

    distance 为片元到相机距离；超过 lod_start 后每跨一个 lod_start
    距离 LOD 等级 +1（丢弃一层高频噪声），到 4×lod_start 处高频细节
    保留量降为 0（仅留低频色块或整体淡出）。
    返回 (LOD 等级, 高频细节保留量 ∈ [0,1])。
    """
    start = max(lod_start, 1e-6)
    d = max(0.0, distance)
    lod_end = start * LOD_RANGE_MULT
    detail = 1.0 - smoothstep(start, lod_end, d)
    lod_level = int(math.floor((d - start) / start)) if d > start else 0
    lod_level = max(0, lod_level)
    return (lod_level, clamp(detail))


# ---------------------------------------------------------------------------
# 10. 相交处理
# ---------------------------------------------------------------------------

def intersect_fix(foam_depth, obstacle_depth):
    """泡沫与礁石相交的软化剔除，取代直接穿模。

    深度约定：值越小离相机越近。delta = 礁石深度 - 泡沫深度：
    礁石更近（delta < 0）时压掉泡沫，泡沫在前（delta > 0）时保留；
    在 ±INTERSECT_BIAS 窗口内 smoothstep 过渡，避免相交线闪烁与
    Z-fighting。返回 (可见度 ∈ [0,1], 泡沫深度)。
    """
    delta = obstacle_depth - foam_depth
    visibility = smoothstep(-INTERSECT_BIAS, INTERSECT_BIAS, delta)
    return (clamp(visibility), foam_depth)
