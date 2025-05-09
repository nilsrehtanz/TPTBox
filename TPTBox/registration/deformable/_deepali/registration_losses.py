from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Generator, Mapping, Sequence
from typing import TypeVar

import torch
from deepali.core import Grid, PaddingMode, Sampling
from deepali.core import functional as deepali_functional
from deepali.losses import (
    BSplineLoss,
    DisplacementLoss,
    LandmarkPointDistance,
    PairwiseImageLoss,
    ParamsLoss,
    PointSetDistance,
    RegistrationLoss,
    RegistrationLosses,
    RegistrationResult,
)
from deepali.modules import SampleImage
from deepali.spatial import BSplineTransform, CompositeTransform, SequentialTransform, SpatialTransform
from torch import Tensor
from torch.nn import Module

r"""Registration loss for pairwise image registration."""

RE_WEIGHT = re.compile(r"^((?P<mul>[0-9]+(\.[0-9]+)?)\s*[\* ])?\s*(?P<chn>[a-zA-Z0-9_-]+)\s*(\+\s*(?P<add>[0-9]+(\.[0-9]+)?))?$")
RE_TERM_VAR = re.compile(r"^[a-zA-Z0-9_-]+\((?P<var>[a-zA-Z0-9_]+)\)$")


TModule = TypeVar("TModule", bound=Module)
TSpatialTransform = TypeVar("TSpatialTransform", bound=SpatialTransform)


class PairwiseImageRegistrationLoss(RegistrationLoss):
    r"""Loss function for pairwise multi-channel image registration."""

    def __init__(
        self,
        source_data: Tensor,
        target_data: Tensor,
        source_grid: Grid,
        target_grid: Grid,
        source_chns: Mapping[str, int | tuple[int, int]],
        target_chns: Mapping[str, int | tuple[int, int]],
        source_pset: Tensor | None = None,
        target_pset: Tensor | None = None,
        source_landmarks: Tensor | None = None,
        target_landmarks: Tensor | None = None,
        losses: RegistrationLosses | None = None,
        weights: Mapping[str, float | str] | None = None,
        transform: CompositeTransform | SpatialTransform | None = None,
        sampling: Sampling | str = Sampling.LINEAR,
    ):
        r"""Initialize multi-channel registration loss function.

        Args:
            source_data: Moving normalized multi-channel source image batch tensor.
            source_data: Fixed normalized multi-channel target image batch tensor.
            source_grid: Sampling grid of source image.
            source_grid: Sampling grid of target image.
            source_chns: Mapping from channel (loss, weight) name to index or range.
            target_chns: Mapping from channel (loss, weight) name to index or range.
            source_pset: Point sets defined with respect to source image grid.
            target_pset: Point sets defined with respect to target image grid.
            source_landmarks: Landmark points defined with respect to source image grid.
            target_landmarks: Landmark points defined with respect to target image grid.
            losses: Dictionary of named loss terms. Loss terms must be either a subclass of
                ``PairwiseImageLoss``, ``DisplacementLoss``, ``PointSetDistance``, ``ParamsLoss``,
                or ``torch.nn.Module``. In case of a ``PairwiseImageLoss``, the key (name) of the
                loss term must be found in ``channels`` which identifies the corresponding ``target``
                and ``source`` data channels that this loss term relates to. If the name is not found
                in the ``channels`` mapping, the loss term is called with all image channels as input.
                If a loss term is not an instance of a known registration loss type, it is assumed to be a
                regularization term without arguments, e.g., a ``torch.nn.Module`` which itself has a reference
                to the parameters of the transformation that it is based on.
            weights: Scalar weights of loss terms or name of channel with locally adaptive weights.
            transform: Spatial transformation to apply to ``source`` image.
            sampling: Image interpolation mode.

        """
        super().__init__()
        self.register_buffer("_source_data", source_data)
        self.register_buffer("_target_data", target_data)
        self.source_grid = source_grid
        self.target_grid = target_grid
        self.source_chns = dict(source_chns or {})
        self.target_chns = dict(target_chns or {})
        self.source_pset = source_pset
        self.target_pset = target_pset
        self.source_landmarks = source_landmarks
        self.target_landmarks = target_landmarks
        if transform is None:
            transform = SequentialTransform(self.target_grid)
        elif isinstance(transform, SpatialTransform):
            transform = SequentialTransform(transform)
        elif not isinstance(transform, CompositeTransform):
            raise TypeError("PairwiseImageRegistrationLoss() 'transform' must be of type CompositeTransform")
        self.transform = transform
        self._sample_image = SampleImage(
            target=self.target_grid,
            source=self.source_grid,
            sampling=sampling,
            padding=PaddingMode.ZEROS,
            align_centers=False,
        )
        points = self.target_grid.coords(device=self._target_data.device)
        self.register_buffer("grid_points", points.unsqueeze(0))
        self.loss_terms = self.as_module_dict(losses)
        self.weights = dict(weights or {})

    @property
    def device(self) -> torch.device:
        r"""Device on which loss is evaluated."""
        device = self._target_data.device
        assert isinstance(device, torch.device)
        return device

    def loss_terms_of_type(self, loss_type: type[TModule]) -> dict[str, TModule]:
        r"""Get dictionary of loss terms of a specifictype."""
        return {name: module for name, module in self.loss_terms.items() if isinstance(module, loss_type)}

    def transforms_of_type(self, transform_type: type[TSpatialTransform]) -> list[TSpatialTransform]:
        r"""Get list of spatial transformations of a specific type."""

        def _iter_transforms(
            transform,
        ) -> Generator[SpatialTransform, None, None]:
            if isinstance(transform, transform_type):
                yield transform
            elif isinstance(transform, CompositeTransform):
                for t in transform.transforms():
                    yield from _iter_transforms(t)

        transforms = list(_iter_transforms(self.transform))
        return transforms  # type: ignore

    @property
    def has_transform(self) -> bool:
        r"""Whether a spatial transformation is set."""
        return len(self.transform) > 0

    def target_data(self) -> Tensor:
        r"""Target image tensor."""
        data = self._target_data
        assert isinstance(data, Tensor)
        return data

    def source_data(self, grid: Tensor | None = None) -> Tensor:
        r"""Sample source image at transformed target grid points."""
        data = self._source_data
        assert isinstance(data, Tensor)
        if grid is None:
            return data
        return self._sample_image(grid, data)

    def data_mask(self, data: Tensor, channels: dict[str, int | tuple[int, int]]) -> Tensor:
        r"""Get boolean mask from data tensor."""
        slice_ = self.as_slice(channels["msk"])
        start, stop = slice_.start, slice_.stop
        mask = data.narrow(1, start, stop - start)
        return mask > 0.9

    def overlap_mask(self, source: Tensor, target: Tensor) -> Tensor | None:
        r"""Overlap mask at which to evaluate pairwise data term."""
        mask = self.data_mask(source, self.source_chns)
        mask &= self.data_mask(target, self.target_chns)
        return mask

    @classmethod
    def as_slice(cls, arg: int | Sequence[int]) -> slice:
        r"""Slice of image data channels associated with the specified name."""
        if isinstance(arg, int):
            arg = (arg,)
        if len(arg) == 1:
            arg = (arg[0], arg[0] + 1)
        if len(arg) == 2:
            arg = (arg[0], arg[1], 1)
        if len(arg) != 3:
            raise ValueError(f"{cls.__name__}.as_slice() 'arg' must be int or sequence of length 1, 2, or 3")
        return slice(*arg)

    @classmethod
    def data_channels(cls, data: Tensor, c: slice) -> Tensor:
        r"""Get subimage data tensor of named channel."""
        i = (slice(0, data.shape[0]), c, *tuple(slice(0, data.shape[dim]) for dim in range(2, data.ndim)))
        return data[i]

    def loss_input(self, name: str, data: Tensor, channels: dict[str, int | tuple[int, int]]) -> Tensor:
        r"""Get input for named loss term."""
        if name in channels:
            c = channels[name]
        elif "img" not in channels:
            raise RuntimeError(f"Channels map contains neither entry for '{name}' nor 'img'")
        else:
            c = channels["img"]
        i: slice = self.as_slice(c)
        return self.data_channels(data, i)

    def loss_mask(
        self,
        name: str,
        data: Tensor,
        channels: dict[str, int | tuple[int, int]],
        mask: Tensor,
    ) -> Tensor:
        r"""Get mask for named loss term."""
        weight = self.weights.get(name, 1.0)
        if not isinstance(weight, str):
            return mask
        match = RE_WEIGHT.match(weight)
        if match is None:
            raise RuntimeError(f"Invalid weight string ('{weight}') for loss term '{name}'")
        chn = match.group("chn")
        mul = match.group("mul")
        add = match.group("add")
        c = channels.get(chn)
        if c is None:
            raise RuntimeError(f"Channels map contains no entry for '{name}' weight string '{weight}'")
        i = self.as_slice(c)
        w = self.data_channels(data, i)
        if mul is not None:
            w = w * float(mul)
        if add is not None:
            w = w + float(add)
        return w * mask

    def eval(self) -> RegistrationResult:  # noqa: C901
        r"""Evaluate pairwise image registration loss."""
        result = {}
        losses = {}
        misc_excl = set()
        # Transform target grid points
        x: Tensor = self.grid_points
        y: Tensor = self.transform(x, grid=True)
        # Buffered vector fields
        variables = defaultdict(list)
        for name, buf in self.transform.named_buffers():
            if buf.requires_grad:
                var = name.rsplit(".", 1)[-1]
                variables[var].append(buf)
        variables["w"] = [deepali_functional.move_dim(y - x, -1, 1)]
        # Sum of pairwise image dissimilarity terms
        data_terms = self.loss_terms_of_type(PairwiseImageLoss)
        misc_excl |= set(data_terms.keys())
        if data_terms:
            source = self.source_data(y)
            target = self.target_data()
            mask = self.overlap_mask(source, target)
            for name, term in data_terms.items():
                s = self.loss_input(name, source, self.source_chns)
                t = self.loss_input(name, target, self.target_chns)
                m = self.loss_mask(name, target, self.target_chns, mask)
                losses[name] = term(s, t, mask=m)
            result["source"] = source
            result["target"] = target
            result["mask"] = mask
        # Sum of pairwise point set distance terms
        dist_terms = self.loss_terms_of_type(PointSetDistance)
        misc_excl |= set(dist_terms.keys())
        ldist_terms = {k: v for k, v in dist_terms.items() if isinstance(v, LandmarkPointDistance)}
        dist_terms = {k: v for k, v in dist_terms.items() if k not in ldist_terms}
        if dist_terms:
            if self.source_pset is None:
                raise RuntimeError(f"{type(self).__name__}() missing source point set")
            if self.target_pset is None:
                raise RuntimeError(f"{type(self).__name__}() missing target point set")
            s = self.source_pset
            t = self.transform(self.target_pset)
            for name, term in dist_terms.items():
                losses[name] = term(t, s)
        if ldist_terms:
            if self.source_landmarks is None:
                raise RuntimeError(f"{type(self).__name__}() missing source landmarks")
            if self.target_landmarks is None:
                raise RuntimeError(f"{type(self).__name__}() missing target landmarks")
            s = self.source_landmarks
            t = self.transform(self.target_landmarks)
            for name, term in ldist_terms.items():
                losses[name] = term(t, s)
        # Sum of displacement field regularization terms
        disp_terms = self.loss_terms_of_type(DisplacementLoss)
        misc_excl |= set(disp_terms.keys())
        for name, term in disp_terms.items():
            match = RE_TERM_VAR.match(name)
            if match:
                var = match.group("var")
            elif "v" in variables:
                var = "v"
            elif "u" in variables:
                var = "u"
            else:
                var = "w"
            bufs = variables.get(var)
            if not bufs:
                raise RuntimeError(f"Unknown variable in loss term name '{name}'")
            value = torch.tensor(0, dtype=torch.float, device=self.device)
            for buf in bufs:
                value += term(buf)
            losses[name] = value
        # Sum of free-form deformation loss terms
        bspline_transforms = self.transforms_of_type(BSplineTransform)
        bspline_terms = self.loss_terms_of_type(BSplineLoss)
        misc_excl |= set(bspline_terms.keys())
        for name, term in bspline_terms.items():
            value = torch.tensor(0, dtype=torch.float, device=self.device)
            for bspline_transform in bspline_transforms:
                value += term(bspline_transform.data())
            losses[name] = value
        # Sum of parameters loss terms
        params_terms = self.loss_terms_of_type(ParamsLoss)
        misc_excl |= set(params_terms.keys())
        for name, term in params_terms.items():
            value = torch.tensor(0, dtype=torch.float, device=self.device)
            count = 0
            for params in self.transform.parameters():
                value += term(params)
                count += 1
            if count > 1:
                value /= count
            losses[name] = value
        # Sum of other regularization terms
        misc_terms = {k: v for k, v in self.loss_terms.items() if k not in misc_excl}
        for name, term in misc_terms.items():
            losses[name] = term()
        # Calculate total loss
        result["losses"] = losses
        result["weights"] = self.weights
        result["loss"] = self._weighted_sum(losses)
        return result

    def _weighted_sum(self, losses: Mapping[str, Tensor]) -> Tensor:
        r"""Compute weighted sum of loss terms."""
        loss = torch.tensor(0, dtype=torch.float, device=self.device)
        weights = self.weights
        for name, value in losses.items():
            w = weights.get(name, 1.0)
            if not isinstance(w, str):
                value = w * value  # noqa: PLW2901
            loss += value.sum()
        return loss


def weight_channel_names(weights: Mapping[str, float | str]) -> dict[str, str]:
    r"""Get names of channels that are used to weight loss term of another channel."""
    names = {}
    for term, weight in weights.items():
        if not isinstance(weight, str):
            continue
        match = RE_WEIGHT.match(weight)
        if match is None:
            continue
        names[term] = match.group("chn")
    return names
