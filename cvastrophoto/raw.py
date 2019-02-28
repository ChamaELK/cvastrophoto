# -*- coding: utf-8 -*-
import os.path
import rawpy
import numpy
import scipy.stats
import PIL.Image
import functools

import logging

logger = logging.getLogger('cvastrophoto.raw')

class Raw(object):

    def __init__(self, path,
            demosaic_algorithm=rawpy.DemosaicAlgorithm.DHT,
            default_pool=None):
        self.name = path
        self.default_pool = default_pool
        self.postprocessing_params = rawpy.Params(
            output_bps=16,
            no_auto_bright=True,
            demosaic_algorithm=demosaic_algorithm,
        )
        self._rimg = None
        self._postprocessed = None

    def close(self):
        if self._rimg is not None:
            self._rimg.close()
            self._rimg = None
        self._postprocessed = None

    @classmethod
    def open_all(cls, dir_path, **kw):
        rv = []
        for path in os.listdir(dir_path):
            fullpath = os.path.join(dir_path, path)
            if os.path.isfile(fullpath):
                rv.append(cls(fullpath, **kw))
        return rv

    @property
    def rimg(self):
        if self._rimg is None:
            self._rimg = rawpy.imread(self.name)
        return self._rimg

    def postprocess(self, **kwargs):
        self._postprocessed = self.rimg.postprocess(self.postprocessing_params)
        return self._postprocessed

    @property
    def postprocessed(self):
        if self._postprocessed is None:
            self._postprocessed = self.postprocess()
        return self._postprocessed

    def show(self):
        postprocessed = self.postprocessed
        PIL.Image.fromarray(numpy.clip(
            postprocessed >> 8,
            0, 255,
            out=numpy.empty(postprocessed.shape, numpy.uint8)
        )).show()

    def save(self, path, *p, **kw):
        postprocessed = self.postprocessed
        PIL.Image.fromarray(numpy.clip(
            postprocessed >> 8,
            0, 255,
            out=numpy.empty(postprocessed.shape, numpy.uint8)
        )).save(path, *p, **kw)

    def denoise(self, darks, pool=None):
        if pool is None:
            pool = self.default_pool
        logger.info("Denoising %s", self)
        raw_image = self.rimg.raw_image
        for dark, k_num, k_denom in find_entropy_weights(self, darks, pool=pool):
            logger.debug("Applying %s with weight %d/%d", dark, k_num, k_denom)
            dark_weighed = dark.rimg.raw_image.astype(numpy.uint32)
            dark_weighed *= k_num
            dark_weighed /= k_denom
            dark_weighed = numpy.minimum(dark_weighed, raw_image, out=dark_weighed)
            raw_image -= dark_weighed
        logger.info("Finished denoising %s", self)

    def __str__(self):
        return self.name

    def __repr__(self):
        return '%s(%r)' % (type(self).__name__, self.name)

class RawAccumulator(object):

    def __init__(self):
        self.accum = None
        self.num_images = 0

    def __iadd__(self, raw):
        if self.accum is None:
            self.accum = raw.rimg.raw_image.astype(numpy.uint32)
            self.num_images = 1
        else:
            self.accum += raw.rimg.raw_image
            self.num_images += 1
        return self

    @property
    def average(self):
        if self.accum is not None:
            return self.accum / self.num_images

    @property
    def raw_image(self):
        accum = self.accum
        if accum is not None:
            maxval = accum.max()
            shift = 0
            while maxval > 65535:
                shift += 1
                maxval /= 2

            if shift:
                accum = accum >> shift
            return accum.astype(numpy.uint16)

def entropy(light, dark, k_denom, k_num, scratch=None, return_params=False):
    if scratch is None:
        scratch = numpy.empty(light.rimg.raw_image.shape, numpy.int32)
    scratch[:] = light.rimg.raw_image
    dark_weighed = dark.rimg.raw_image.astype(numpy.int32)
    dark_weighed *= k_num
    dark_weighed /= k_denom
    scratch -= dark_weighed
    scratch = numpy.absolute(scratch, out=scratch)
    labels, counts = numpy.unique(scratch, return_counts=True)
    rv = scipy.stats.entropy(counts)
    if return_params:
        rv = rv, k_num, k_denom
    return rv

def _refine_entropy(light, dark, steps, denom, base, pool=None):
    base *= steps
    denom *= steps
    _entropy = functools.partial(entropy, light, dark, denom, return_params=True)
    if pool is None:
        dark_ranges = map(_entropy, xrange(base, base + steps))
    else:
        dark_ranges = pool.map(_entropy, xrange(base, base + steps))
    return min(dark_ranges)

def find_entropy_weights(light, darks, steps=8, maxsteps=512, pool=None, mink = 0.01):
    ranges = []
    for dark in darks:
        initial_range = _refine_entropy(light, dark, steps, 1, 0, pool=pool)
        ranges.append((initial_range, dark))

    while ranges:
        best = min(ranges)
        ranges.remove(best)
        (e, base, denom), dark = best

        refined_range = _refine_entropy(light, dark, steps, denom, base, pool=pool)
        e, base, denom = refined_range

        if denom >= maxsteps:
            yield dark, base, denom

            if base / float(denom) < mink:
                # Close enough
                break

            # Reset remaining ranges
            ranges = [
                (_refine_entropy(light, dark, steps, 1, 0, pool=pool), dark)
                for (_, dark) in ranges
            ]
        else:
            ranges.append((refined_range, dark))

