"""Bounded float64 CUDA difference detection with the CPU extractor's rules."""

from collections import OrderedDict

import numpy as np
import torch
import torch.nn.functional as functional

from classcue import slide_extractor as rules


class CudaDifference:
    def __init__(self):
        if not torch.cuda.is_available():
            raise RuntimeError("PPT CUDA 不可用")
        free, _ = torch.cuda.mem_get_info()
        if free < 384 * 2**20:
            raise RuntimeError("PPT CUDA 显存不足，请释放显存后重试")
        self.frames = OrderedDict()
        self.graphs = OrderedDict()
        self.indices = {}
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False

    def clear_frames(self):
        self.frames.clear()
        for entry in self.graphs.values():
            entry[-1][:] = [None] * len(entry[0])
        # Captured graphs keep their own references to the index tensors.
        self.indices.clear()

    def _index(self, size):
        if size not in self.indices:
            index = torch.arange(-3, size + 3, device="cuda") % (2 * size)
            self.indices[size] = torch.where(index < size, index, 2 * size - 1 - index)
        return self.indices[size]

    def _box(self, value):
        height, width = value.shape[-2:]
        vertical = value.index_select(-2, self._index(height))
        vertical = functional.avg_pool2d(vertical, (7, 1), stride=1)
        return functional.avg_pool2d(
            vertical.index_select(-1, self._index(width)), (1, 7), stride=1,
        )

    def _frame(self, feature):
        key = id(feature)
        if key in self.frames and self.frames[key][0] is feature:
            self.frames.move_to_end(key)
            return self.frames[key][1]
        gray = torch.from_numpy(feature["gray"]).to(device="cuda", dtype=torch.float64)[None, None]
        moments = self._box(torch.cat((gray, gray * gray), dim=0))
        mean, square = moments[0:1], moments[1:2]
        variance = (49 / 48) * (square - mean * mean)
        rgb = torch.from_numpy(feature["rgb"]).to(device="cuda", dtype=torch.int16)
        valid = torch.from_numpy(feature["valid"]).to(device="cuda")
        data = gray, mean, variance, rgb, valid
        self.frames[key] = feature, data
        while len(self.frames) > 8:
            self.frames.popitem(last=False)
        return data

    def _graph(self, first, second):
        shape = tuple(first[0].shape[-2:])
        if shape in self.graphs:
            self.graphs.move_to_end(shape)
            return self.graphs[shape]
        # Device inputs and indices must outlive every graph replay.
        inputs = tuple(torch.zeros_like(value) for value in (*first, *second))
        inputs[4].fill_(True)
        inputs[9].fill_(True)
        height, width = shape
        self._index(height)
        self._index(width)
        indices = self.indices[height], self.indices[width]

        def calculate():
            x, ux, vx, rgb_x, _, y, uy, vy, rgb_y, valid = inputs
            covariance = (49 / 48) * (self._box(x * y) - ux * uy)
            similarity = (((2 * ux * uy + 2.55**2) * (2 * covariance + 7.65**2)) /
                          ((ux**2 + uy**2 + 2.55**2) * (vx + vy + 7.65**2)))[0, 0]
            delta = (rgb_x - rgb_y).abs().amax(dim=2)
            raw = delta > rules.PIXEL_THRESHOLD
            mask = (raw & ((similarity < rules.STRUCTURE_THRESHOLD) |
                           (delta > rules.COLOR_CHANGE_THRESHOLD)) & valid).to(torch.uint8)
            stats = torch.stack((raw.sum().to(torch.float64),
                                 (similarity * valid).sum() / valid.sum()))
            return mask, stats

        side = torch.cuda.Stream()
        side.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(side):
            for _ in range(3):
                calculate()
        torch.cuda.current_stream().wait_stream(side)
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph):
            mask, stats = calculate()
        entry = inputs, graph, mask, stats, indices, [None] * len(inputs)
        self.graphs[shape] = entry
        while len(self.graphs) > 2:
            self.graphs.popitem(last=False)
        return entry

    def __call__(self, previous, current):
        with torch.inference_mode():
            first, second = self._frame(previous), self._frame(current)
            inputs, graph, mask, stats, _, previous_inputs = self._graph(first, second)
            for index, (target, value) in enumerate(zip(inputs, (*first, *second))):
                if previous_inputs[index] is not value:
                    target.copy_(value)
                    previous_inputs[index] = value
            graph.replay()
            values = stats.cpu().numpy()
            cpu_mask = mask.cpu().numpy()
        return rules._summarize_difference(current, cpu_mask, int(values[0]), float(values[1]))
