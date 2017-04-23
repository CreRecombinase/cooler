# -*- coding: utf-8 -*-
"""
Contact Binners
~~~~~~~~~~~~~~~

Binners are iterators that convert input data of various flavors into a 
properly sorted, chunked stream of binned contacts.

"""
from __future__ import division, print_function
from collections import OrderedDict, Counter
from bisect import bisect_left
from multiprocess import Pool
import itertools
import warnings
import sys
import six

#from pandas.algos import is_lexsorted
import numpy as np
import pandas
import h5py

from . import get_logger
from .util import rlencode, get_binsize, parse_region
from .io import parse_cooler_uri
from .tools import lock, partition


logger = get_logger()


class ContactBinner(object):
    """
    Interface of a contact reader.

    """
    def size(self):
        """ Returns the total number of contacts.  **DEPRECATED** """
        raise NotImplementedError

    def __iter__(self):
        """ Iterator over chunks of binned contacts

        Chunks are expected to have the following format:

        * dict of 1D arrays
        * keys `bin1_id`, `bin2_id`, `count`
        * arrays lexically sorted by `bin_id` then `bin2_id`

        """
        raise NotImplementedError


def check_bins(bins, chromsizes):
    if bins['chrom'].dtype.name != 'category':
        bins['chrom'] = pandas.Categorical(
            bins.chrom, 
            categories=list(chromsizes.index), 
            ordered=True)
    else:
        assert (bins['chrom'].cat.categories == chromsizes.index).all()

    return bins


class GenomeSegmentation(object):
    def __init__(self, chromsizes, bins):
        bins = check_bins(bins, chromsizes)
        self._bins_grouped = bins.groupby('chrom', sort=False)
        nbins_per_chrom = self._bins_grouped.size().values
        
        self.chromsizes = chromsizes
        self.binsize = get_binsize(bins)
        self.contigs = list(chromsizes.keys())
        self.bins = bins
        self.idmap = pandas.Series(
            index=chromsizes.keys(), 
            data=range(len(chromsizes)))
        self.chrom_binoffset = np.r_[0, np.cumsum(nbins_per_chrom)]
        self.chrom_abspos = np.r_[0, np.cumsum(chromsizes.values)]
        self.start_abspos = (self.chrom_abspos[bins['chrom'].cat.codes] + 
                             bins['start'].values)
    
    def fetch(self, region):
        chrom, start, end = parse_region(region, self.chromsizes)
        result = self._bins_grouped.get_group(chrom)
        if start > 0 or end < self.chromsizes[chrom]:
            lo = result['end'].values.searchsorted(start, side='right')
            hi = lo + result['start'].values[lo:].searchsorted(end, side='left')
            result = result.iloc[lo:hi]
        return result


class HDF5Aggregator(ContactBinner):
    """
    Aggregate contacts from a hiclib-style HDF5 contacts file.

    """
    def __init__(self, h5pairs, chromsizes, bins, chunksize, **kwargs):
        self.h5 = h5pairs
        self.C1 = kwargs.pop('C1', 'chrms1')
        self.P1 = kwargs.pop('P1', 'cuts1')
        self.C2 = kwargs.pop('C2', 'chrms2')
        self.P2 = kwargs.pop('P2', 'cuts2')
        self.gs = GenomeSegmentation(chromsizes, bins)
        self.chunksize = chunksize
        self.partition = self._index_chroms()

    def _index_chroms(self):
        # index extents of chromosomes on first axis of contact list
        starts, lengths, values = rlencode(self.h5[self.C1], self.chunksize)
        if len(set(values)) != len(values):
            raise ValueError(
                "Read pair coordinates are not sorted on the first axis")
        return dict(zip(values, zip(starts, starts + lengths)))

    def _load_chunk(self, lo, hi):
        data = OrderedDict([
            ('chrom_id1', self.h5[self.C1][lo:hi]),
            ('cut1', self.h5[self.P1][lo:hi]),
            ('chrom_id2', self.h5[self.C2][lo:hi]),
            ('cut2', self.h5[self.P2][lo:hi]),
        ])
        return pandas.DataFrame(data)

    def aggregate(self, chrom):
        h5pairs = self.h5
        C1, P1, C2, P2 = self.C1, self.P1, self.C2, self.P2
        chunksize = self.chunksize
        bins = self.gs.bins
        binsize = self.gs.binsize
        chrom_binoffset = self.gs.chrom_binoffset
        chrom_abspos = self.gs.chrom_abspos
        start_abspos = self.gs.start_abspos
        cid = self.gs.idmap[chrom]

        chrom_lo, chrom_hi = self.partition.get(cid, (-1, -1))
        lo = chrom_lo
        hi = lo
        while hi < chrom_hi:
            # update `hi` to make sure our selection doesn't split a bin1
            lo, hi = hi, min(hi + chunksize, chrom_hi)
            abspos = chrom_abspos[cid] + h5pairs[P1][hi - 1]
            bin_id = int(np.searchsorted(
                start_abspos, abspos, side='right')) - 1
            bin_end = bins['end'][bin_id]
            hi = bisect_left(h5pairs[P1], bin_end, lo, chrom_hi)
            if lo == hi:
                hi = chrom_hi

            logger.info('{} {}'.format(lo, hi))

            # load chunk and assign bin IDs to each read side
            table = self._load_chunk(lo, hi)
            abspos1 = chrom_abspos[h5pairs[C1][lo:hi]] + h5pairs[P1][lo:hi]
            abspos2 = chrom_abspos[h5pairs[C2][lo:hi]] + h5pairs[P2][lo:hi]
            if np.any(abspos1 > abspos2):
                raise ValueError(
                    "Found a read pair that maps to the lower triangle of the "
                    "contact map (side1 > side2). Check that the provided "
                    "chromosome ordering and read pair file are consistent "
                    "such that all pairs map to the upper triangle with "
                    "respect to the given chromosome ordering.")

            if binsize is None:
                table['bin1_id'] = np.searchsorted(
                    start_abspos, abspos1, side='right') - 1
                table['bin2_id'] = np.searchsorted(
                    start_abspos, abspos2, side='right') - 1
            else:
                rel_bin1 = np.floor(table['cut1']/binsize).astype(int)
                rel_bin2 = np.floor(table['cut2']/binsize).astype(int)
                table['bin1_id'] = (
                    chrom_binoffset[table['chrom_id1'].values] + rel_bin1)
                table['bin2_id'] = (
                    chrom_binoffset[table['chrom_id2'].values] + rel_bin2)

            # reduce
            gby = table.groupby(['bin1_id', 'bin2_id'])
            agg = (gby['chrom_id1'].count()
                                   .reset_index()
                                   .rename(columns={'chrom_id1': 'count'}))
            yield agg

    def size(self):
        return len(self.h5['chrms1'])

    def __iter__(self):
        for chrom in self.gs.contigs:
            for df in self.aggregate(chrom):
                yield {k: v.values for k, v in six.iteritems(df)}


class TabixAggregator(ContactBinner):
    """
    Aggregate contacts from a sorted, BGZIP-compressed and tabix-indexed
    tab-delimited text file.

    """
    def __init__(self, filepath, chromsizes, bins, map=map, **kwargs):
        try:
            import pysam
        except ImportError:
            raise ImportError("pysam is required to read tabix files")
        
        self._map = map
        self.C2 = kwargs.pop('C2', 3)
        self.P2 = kwargs.pop('P2', 4)
        
        # all requested contigs will be placed in the output matrix
        self.gs = GenomeSegmentation(chromsizes, bins)
        
        # find available contigs in the contact list
        self.filepath = filepath
        self.n_records = None
        with pysam.TabixFile(filepath, 'r', encoding='ascii') as f:
            try:
                self.file_contigs = [c.decode('ascii') for c in f.contigs]
            except AttributeError:
                self.file_contigs = f.contigs
        
        # warn about requested contigs not seen in the contact list
        for chrom in self.gs.contigs:
            if chrom not in self.file_contigs:
                warnings.warn(
                    "Did not find contig " +
                    " '{}' in contact list file.".format(chrom))
        
        warnings.warn(
            "NOTE: When using the Tabix aggregator, make sure the order of "
            "chromosomes in the provided chromsizes agrees with the chromosome "
            "ordering of read ends in the contact list file.")

    def __getstate__(self):
        d = self.__dict__.copy()
        d.pop('_map', None)
        return d

    def _size(self, chrom):
        import pysam
        with pysam.TabixFile(self.filepath, 'r', encoding='ascii') as f:
            return sum(1 for line in f.fetch(chrom))
    
    def size(self):
        if self.n_records is None:
            chroms = [ctg for ctg in self.gs.contigs 
                            if ctg in self.file_contigs]
            self.n_records = sum(self._map(self._size, chroms))
        return self.n_records
    
    def aggregate(self, chrom):
        import pysam
        filepath = self.filepath
        binsize = self.gs.binsize
        idmap = self.gs.idmap
        chromsizes = self.gs.chromsizes
        chrom_binoffset = self.gs.chrom_binoffset
        chrom_abspos = self.gs.chrom_abspos
        start_abspos = self.gs.start_abspos
        C2, P2 = self.C2, self.P2
        
        these_bins = self.gs.fetch(chrom)
        rows = []
        with pysam.TabixFile(filepath, 'r', encoding='ascii') as f:
            parser = pysam.asTuple()
            accumulator = Counter()
            
            for bin1_id, bin1 in these_bins.iterrows():
                for line in f.fetch(chrom, bin1.start, bin1.end,
                                    parser=parser):
                    chrom2 = line[C2]
                    pos2 = int(line[P2])
                    
                    try:
                        cid2 = idmap[chrom2]
                    except KeyError:
                        # this chrom2 is not requested
                        continue
                    
                    if binsize is None:
                        lo = chrom_binoffset[cid2]
                        hi = chrom_binoffset[cid2 + 1]
                        bin2_id = lo + np.searchsorted(
                            start_abspos[lo:hi], 
                            chrom_abspos[cid2] + pos2,
                            side='right') - 1
                    else:
                        bin2_id = chrom_binoffset[cid2] + (pos2 // binsize)
                    
                    accumulator[bin2_id] += 1

                if not accumulator:
                    continue

                rows.append(
                    pandas.DataFrame({
                        'bin1_id': bin1_id,
                        'bin2_id': list(accumulator.keys()),
                        'count':   list(accumulator.values())},
                        columns=['bin1_id', 'bin2_id', 'count'])
                          .sort_values('bin2_id')
                )
                
                accumulator.clear()
        
        logger.info(chrom)
        return pandas.concat(rows, axis=0) if len(rows) else None
    
    def __iter__(self):
        chroms = [ctg for ctg in self.gs.contigs if ctg in self.file_contigs]
        for df in self._map(self.aggregate, chroms):
            if df is not None:
                yield {k: v.values for k, v in six.iteritems(df)}


class PairixAggregator(ContactBinner):
    """
    Aggregate contacts from a sorted, BGZIP-compressed and pairix-indexed
    tab-delimited text file.

    """
    def __init__(self, filepath, chromsizes, bins, map=map, **kwargs):
        try:
            import pypairix
        except ImportError:
            raise ImportError(
                "pypairix is required to read pairix-indexed files")
        
        self._map = map
        f = pypairix.open(filepath, 'r')
        self.C1 = f.get_chr1_col()
        self.C2 = f.get_chr2_col()
        self.P1 = f.get_startpos1_col()
        self.P2 = f.get_startpos2_col()
        self.file_contigs = set(
            itertools.chain.from_iterable(
                [b.split('|') for b in f.get_blocknames()]))
        
        # all requested contigs will be placed in the output matrix
        self.gs = GenomeSegmentation(chromsizes, bins)
        
        # find available contigs in the contact list
        self.filepath = filepath
        self.n_records = None
       
        # warn about requested contigs not seen in the contact list
        for chrom in self.gs.contigs:
            if chrom not in self.file_contigs:
                warnings.warn(
                    "Did not find contig " +
                    " '{}' in contact list file.".format(chrom))

    def __getstate__(self):
        d = self.__dict__.copy()
        d.pop('_map', None)
        return d

    def _size(self, block):
        import pypairix
        f = pypairix.open(self.filepath, 'r')
        chrom1, chrom2 = block
        return sum(1 for line in f.query2D(
            chrom1, 0, self.gs.chromsizes[chrom1],
            chrom2, 0, self.gs.chromsizes[chrom2], 1))
    
    def size(self):
        if self.n_records is None:
            chroms = [ctg for ctg in self.gs.contigs 
                            if ctg in self.file_contigs]
            blocks = itertools.combinations_with_replacement(chroms, 2)
            self.n_records = sum(self._map(self._size, blocks))
        return self.n_records
    
    def aggregate(self, chrom1):
        import pypairix
        filepath = self.filepath
        binsize = self.gs.binsize
        chromsizes = self.gs.chromsizes
        chrom_binoffset = self.gs.chrom_binoffset
        chrom_abspos = self.gs.chrom_abspos
        start_abspos = self.gs.start_abspos
        C1 = self.C1
        C2 = self.C2
        P1 = self.P1
        P2 = self.P2
        
        f = pypairix.open(filepath, 'r')
        these_bins = self.gs.fetch(chrom1)
        remaining_chroms = self.gs.idmap[chrom1:]
        cid1 = self.gs.idmap[chrom1]

        accumulator = Counter()
        rows = []
        for bin1_id, bin1 in these_bins.iterrows():
            
            for chrom2, cid2 in six.iteritems(remaining_chroms):
                
                chrom2_size = chromsizes[chrom2]

                if chrom1 != chrom2 and f.exists2(chrom2, chrom1):  # flipped
                    iterator = f.query2D(chrom2, 0, chrom2_size, 
                                         chrom1, bin1.start, bin1.end)
                    pos2_col = P1
                else:
                    iterator = f.query2D(chrom1, bin1.start, bin1.end, 
                                         chrom2, 0, chrom2_size)
                    pos2_col = P2

                for line in iterator:
                    
                    pos2 = int(line[pos2_col])

                    if binsize is None:
                        lo = chrom_binoffset[cid2]
                        hi = chrom_binoffset[cid2 + 1]
                        bin2_id = lo + np.searchsorted(
                            start_abspos[lo:hi], 
                            chrom_abspos[cid2] + pos2,
                            side='right') - 1
                    else:
                        bin2_id = chrom_binoffset[cid2] + (pos2 // binsize)
                    
                    accumulator[bin2_id] += 1
            
            if not accumulator:
                continue
            
            rows.append(
                pandas.DataFrame({
                    'bin1_id': bin1_id,
                    'bin2_id': list(accumulator.keys()),
                    'count':   list(accumulator.values())},
                    columns=['bin1_id', 'bin2_id', 'count'])
                      .sort_values('bin2_id')
            )
            
            accumulator.clear()
        
        logger.info(chrom1)

        return pandas.concat(rows, axis=0) if len(rows) else None
    
    def __iter__(self):
        chroms = [ctg for ctg in self.gs.contigs if ctg in self.file_contigs]
        for df in self._map(self.aggregate, chroms):
            if df is not None:
                yield {k: v.values for k, v in six.iteritems(df)}


class CoolerAggregator(ContactBinner):
    """
    Aggregate contacts from an existing Cooler file.

    """
    def __init__(self, source_uri, bins, chunksize, batchsize, map=map):
        from cooler.api import Cooler
        self._map = map
        self.source_uri = source_uri
        self.chunksize = chunksize
        self.batchsize = batchsize

        clr = Cooler(source_uri)
        self._size = clr.info['nnz']
        self.old_binsize = clr.binsize
        self.old_chrom_offset = clr._load_dset('indexes/chrom_offset')
        self.old_bin1_offset = clr._load_dset('indexes/bin1_offset')
        self.gs = GenomeSegmentation(clr.chromsizes, bins)
        self.new_binsize = get_binsize(bins)
        assert self.new_binsize % self.old_binsize == 0
        self.factor = self.new_binsize // self.old_binsize

    def __getstate__(self):
        d = self.__dict__.copy()
        d.pop('_map', None)
        return d

    def size(self):
        return self._size
    
    def _aggregate(self, span):
        from cooler.api import Cooler
        lo, hi = span

        clr = Cooler(self.source_uri)
        # convert_enum=False returns chroms as raw ints
        table = clr.pixels(join=True, convert_enum=False)
        chunk = table[lo:hi]
        logger.info('{} {}'.format(lo, hi))

        # use the "start" point as anchor for re-binning
        # XXX - alternatives: midpoint anchor, proportional re-binning
        binsize = self.gs.binsize
        chrom_binoffset = self.gs.chrom_binoffset
        chrom_abspos = self.gs.chrom_abspos
        start_abspos = self.gs.start_abspos

        chrom_id1 = chunk['chrom1'].values
        chrom_id2 = chunk['chrom2'].values
        start1 = chunk['start1'].values
        start2 = chunk['start2'].values
        if binsize is None:
            abs_start1 = chrom_abspos[chrom_id1] + start1
            abs_start2 = chrom_abspos[chrom_id2] + start2
            chunk['bin1_id'] = np.searchsorted(
                start_abspos, 
                abs_start1, 
                side='right') - 1
            chunk['bin2_id'] = np.searchsorted(
                start_abspos, 
                abs_start2, 
                side='right') - 1
        else:
            rel_bin1 = np.floor(start1/binsize).astype(int)
            rel_bin2 = np.floor(start2/binsize).astype(int)
            chunk['bin1_id'] = chrom_binoffset[chrom_id1] + rel_bin1
            chunk['bin2_id'] = chrom_binoffset[chrom_id2] + rel_bin2

        grouped = chunk.groupby(['bin1_id', 'bin2_id'], sort=False)
        return grouped['count'].sum().reset_index()

    def aggregate(self, span):
        try:
            chunk = self._aggregate(span)
        except MemoryError as e:
            raise RuntimeError(str(e))
        return chunk

    def __iter__(self):
        old_chrom_offset = self.old_chrom_offset
        old_bin1_offset = self.old_bin1_offset
        chunksize = self.chunksize
        batchsize = self.batchsize
        factor = self.factor
        
        # Partition pixels into chunks, respecting chrom1 boundaries
        spans = []
        for chrom, i in six.iteritems(self.gs.idmap):
            # it's important to extract some multiple of `factor` rows at a time
            c0 = old_chrom_offset[i]
            c1 = old_chrom_offset[i + 1]
            step = (chunksize // factor) * factor
            edges = np.arange(
                old_bin1_offset[c0], 
                old_bin1_offset[c1] + step, 
                step)
            edges[-1] = old_bin1_offset[c1]
            spans.append(zip(edges[:-1], edges[1:]))
        spans = list(itertools.chain.from_iterable(spans))
        
        # Process batches of k chunks at a time, then yield the results
        for i in range(0, len(spans), batchsize):
            try:
                lock.acquire()
                results = self._map(self.aggregate, spans[i:i+batchsize])
            finally:
                lock.release()
            for df in results:
                yield {k: v.values for k, v in six.iteritems(df)}


class CoolerMerger(ContactBinner):
    """
    Merge (i.e. sum) multiple cooler matrices with identical axes.

    """
    def __init__(self, coolers, chunksize, **kwargs):
        self.coolers = list(coolers)
        self.chunksize = chunksize

        binsize = coolers[0].binsize
        if binsize is not None:
            if len(set(c.binsize for c in coolers)) > 1:
                raise ValueError("Coolers must have the same resolution")
            chromsizes = coolers[0].chromsizes
            for i in range(1, len(coolers)):
                if not np.all(coolers[i].chromsizes == chromsizes):
                    raise ValueError("Coolers must have the same chromosomes")
        else:
            bins = coolers[0].bins()[['chrom', 'start', 'end']][:]
            for i in range(1, len(coolers)):
                if not np.all(
                    coolers[i].bins()[['chrom', 'start', 'end']][:] == bins):
                    raise ValueError("Coolers must have same bin structure")

    def size(self):
        return np.sum(c.info['nnz'] for c in self.coolers)

    def __iter__(self):
        chunksize = self.chunksize
        indexes = [c._load_dset('indexes/bin1_offset') for c in self.coolers]
        nnzs = [len(c.pixels()) for c in self.coolers]
        logger.info('nnzs: {}'.format(nnzs))

        lo = 0
        starts = [0] * len(self.coolers)
        while True:
            hi = max(bisect_left(o[:-1], min(start + chunksize, nnz), lo=lo) 
                                 for start, nnz, o in zip(starts, nnzs, indexes))
            if hi == lo:
                break
            stops = [o[hi] for o in indexes]
            logger.info('current: {}'.format(stops))
            
            combined = pandas.concat(
                [c.pixels()[start:stop] 
                    for c, start, stop in zip(self.coolers, starts, stops)],
                axis=0,
                ignore_index=True)

            df = (combined.groupby(['bin1_id', 'bin2_id'], sort=True)
                          .aggregate({'count': np.sum})
                          .reset_index())
            yield {k: v.values for k, v in six.iteritems(df)}

            lo = hi
            starts = stops


class SparseLoader(ContactBinner):
    """
    Load binned contacts from a single 3-column sparse matrix text file.

    """
    def __init__(self, filepath, chunksize):
        """
        Parameters
        ----------
        filepath : str
            Path to tsv file
        chunksize : number of rows of the matrix file to read at a time

        """
        self.iterator = pandas.read_csv(
            filepath, 
            sep='\t', 
            iterator=True,
            chunksize=chunksize,
            names=['bin1_id', 'bin2_id', 'count'])

    def __iter__(self):
        for chunk in self.iterator:
            yield {k: v.values for k,v in six.iteritems(chunk)}


class BedGraph2DLoader(ContactBinner):
    """
    Contact iterator for a sparse tsv Hi-C matrix with fields:
        "chrom1, start1, end1, chrom2, start2, end2, count"
    
    The fields are assumed to be defined and records assumed to 
    be sorted consistently with the bin table provided.
    
    """
    def __init__(self, filepath, bins, chunksize):
        """
        Parameters
        ----------
        filepath : str
            Path to tsv file
        bins : DataFrame
            A bin table dataframe
        chunksize : number of rows of the matrix file to read at a time

        """
        self.iterator = pandas.read_csv(
            filepath, 
            sep='\t', 
            iterator=True,
            chunksize=chunksize,
            names=['chrom1', 'start1', 'end1', 
                   'chrom2', 'start2', 'end2', 'count'])
        self.bins = bins
        self.chunksize = chunksize

    def __iter__(self):
        bins = self.bins
        iterator = self.iterator
        bins['bin'] = bins.index
        
        for chunk in iterator:
            # assign bin IDs from bin table
            df = (chunk.merge(bins, 
                              left_on=['chrom1', 'start1', 'end1'], 
                              right_on=['chrom', 'start', 'end'])
                       .merge(bins, 
                              left_on=['chrom2', 'start2', 'end2'], 
                              right_on=['chrom', 'start', 'end'], 
                              suffixes=('1', '2')))
            df = (df[['bin1', 'bin2', 'count']]
                      .rename(columns={'bin1': 'bin1_id', 
                                       'bin2': 'bin2_id'})
                      .sort_values(['bin1_id', 'bin2_id']))
            yield {k: v.values for k,v in six.iteritems(df)}


class DenseLoader(ContactBinner):
    """
    Load a dense genome-wide numpy array contact matrix.
    TODO: support dask array and/or memmapped arrays

    """
    def __init__(self, heatmap):
        # TRIU sparsify the matrix
        i, j = np.nonzero(heatmap)
        mask = i <= j
        triu_i, triu_j = i[mask], j[mask]
        self.data = {
            'bin1_id': triu_i,
            'bin2_id': triu_j,
            'count': heatmap[triu_i, triu_j],
        }
        self.nnz = len(triu_i)

    def size(self):
        return self.nnz

    def __iter__(self):
        yield self.data