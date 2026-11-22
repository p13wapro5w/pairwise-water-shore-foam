# pairwise-water-shore-foam

## 领域

游戏、图形与多媒体

## 主题

岸边泡沫

## 说明

随潮汐更新、浪高驱动与边缘淡出。

初始状态下 `render/water_shore_foam.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
