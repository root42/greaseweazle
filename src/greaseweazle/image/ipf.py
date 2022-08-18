# greaseweazle/image/ipf.py
#
# Written & released by Keir Fraser <keir.xen@gmail.com>
# 
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

import os, sys
import platform
import ctypes as ct
import itertools as it
from bitarray import bitarray
from greaseweazle.track import MasterTrack, RawTrack
from greaseweazle import error
from .image import Image

class CapsDateTimeExt(ct.Structure):
    _pack_ = 1
    _fields_ = [
        ('year',  ct.c_uint),
        ('month', ct.c_uint),
        ('day',   ct.c_uint),
        ('hour',  ct.c_uint),
        ('min',   ct.c_uint),
        ('sec',   ct.c_uint),
        ('tick',  ct.c_uint)]

class CapsImageInfo(ct.Structure):
    platform_name = [
        "N/A", "Amiga", "Atari ST", "IBM PC", "Amstrad CPC",
        "Spectrum", "Sam Coupe", "Archimedes", "C64", "Atari (8-bit)" ]
    _pack_ = 1
    _fields_ = [
        ('type',        ct.c_uint), # image type
        ('release',     ct.c_uint), # release ID
        ('revision',    ct.c_uint), # release revision ID
        ('mincylinder', ct.c_uint), # lowest cylinder number
        ('maxcylinder', ct.c_uint), # highest cylinder number
        ('minhead',     ct.c_uint), # lowest head number
        ('maxhead',     ct.c_uint), # highest head number
        ('crdt',        CapsDateTimeExt), # image creation date.time
        ('platform',    ct.c_uint * 4)] # intended platform(s)

class CapsTrackInfoT2(ct.Structure):
    _pack_ = 1
    _fields_ = [
        ('type',       ct.c_uint), # track type
        ('cylinder',   ct.c_uint), # cylinder#
        ('head',       ct.c_uint), # head#
        ('sectorcnt',  ct.c_uint), # available sectors
        ('sectorsize', ct.c_uint), # sector size, unused
        ('trackbuf',   ct.POINTER(ct.c_ubyte)), # track buffer memory 
        ('tracklen',   ct.c_uint), # track buffer memory length
        ('timelen',    ct.c_uint), # timing buffer length
        ('timebuf',    ct.POINTER(ct.c_uint)), # timing buffer
        ('overlap',    ct.c_int),  # overlap position
        ('startbit',   ct.c_uint), # start position of the decoding
        ('wseed',      ct.c_uint), # weak bit generator data
        ('weakcnt',    ct.c_uint)] # number of weak data areas

class CapsSectorInfo(ct.Structure):
    _pack_ = 1
    _fields_ = [
        ('descdatasize', ct.c_uint), # data size in bits from IPF descriptor
        ('descgapsize', ct.c_uint),  # gap size in bits from IPF descriptor
        ('datasize', ct.c_uint),     # data size in bits from decoder
        ('gapsize', ct.c_uint),      # gap size in bits from decoder
        ('datastart', ct.c_uint),    # data start pos in bits from decoder
        ('gapstart', ct.c_uint),     # gap start pos in bits from decoder
        ('gapsizews0', ct.c_uint),   # gap size before write splice
        ('gapsizews1', ct.c_uint),   # gap size after write splice
        ('gapws0mode', ct.c_uint),   # gap size mode before write splice
        ('gapws1mode', ct.c_uint),   # gap size mode after write splice
        ('celltype', ct.c_uint),     # bitcell type
        ('enctype', ct.c_uint)]      # encoder type

class CapsDataInfo(ct.Structure):
    _pack_ = 1
    _fields_ = [
        ('type', ct.c_uint),  # data type
        ('start', ct.c_uint), # start position
        ('size', ct.c_uint)]  # size in bits

class DI_LOCK:
    INDEX     = 1<<0
    ALIGN     = 1<<1
    DENVAR    = 1<<2
    DENAUTO   = 1<<3
    DENNOISE  = 1<<4
    NOISE     = 1<<5
    NOISEREV  = 1<<6
    MEMREF    = 1<<7
    UPDATEFD  = 1<<8
    TYPE      = 1<<9
    DENALT    = 1<<10
    OVLBIT    = 1<<11
    TRKBIT    = 1<<12
    NOUPDATE  = 1<<13
    SETWSEED  = 1<<14
    def_flags = (DENVAR | UPDATEFD | TYPE | OVLBIT | TRKBIT)

class IPFTrack(MasterTrack):

    verify_revs = 2
    tolerance = 100

    @staticmethod
    def strong_data(sector, weak):
        """Return list of sector data areas excluding weak sections."""
        def range_next(i):
            s,l = next(i)
            return s, s+l
        weak_tol = 16 # Skip this number of bits after a weak area
        weak_iter = it.chain(weak, [(1<<30,1)])
        ws,we = -1,-1
        sector_iter = iter(sector)
        s,e = range_next(sector_iter)
        try:
            while True:
                while we <= s:
                    ws,we = range_next(weak_iter)
                    we += weak_tol
                if ws < e:
                    if s < ws:
                        yield (s,ws-s)
                    s = we
                else:
                    yield (s,e-s)
                    s = e
                if s >= e:
                    s,e = range_next(sector_iter)
        except StopIteration:
            pass

    def verify_track(self, flux):
        flux.cue_at_index()
        raw = RawTrack(clock = self.time_per_rev/len(self.bits), data = flux)
        raw_bits, _ = raw.get_all_data()
        for s,l in IPFTrack.strong_data(self.sectors, self.weak):
            sector = self.bits[s:s+l]
            # Search within an area +/- the pre-defined # bitcells tolerance
            raw_area = raw_bits[max(self.splice + s - self.tolerance, 0)
                                : self.splice + s + l + self.tolerance]
            # All we care about is at least one match (this is a bit fuzzy)
            if next(raw_area.itersearch(sector), None) is None:
                return False
        return True

class IPF(Image):

    read_only = True

    def __init__(self):
        self.lib = get_libcaps()

    def __del__(self):
        try:
            self.lib.CAPSUnlockAllTracks(self.iid)
            self.lib.CAPSUnlockImage(self.iid)
            self.lib.CAPSRemImage(self.iid)
            del(self.iid)
        except AttributeError:
            pass

    def __str__(self):
        pi = self.pi
        s = "IPF Image File:"
        s += "\n SPS ID: %04d (rev %d)" % (pi.release, pi.revision)
        s += "\n Platform: "
        nr_platforms = 0
        for p in pi.platform:
            if p == 0 and nr_platforms != 0:
                break
            if nr_platforms > 0:
                s += ", "
            s += pi.platform_name[p]
            nr_platforms += 1
        s += ("\n Created: %d/%d/%d %02d:%02d:%02d"
             % (pi.crdt.year, pi.crdt.month, pi.crdt.day,
                pi.crdt.hour, pi.crdt.min, pi.crdt.sec))
        s += ("\n Cyls: %d-%d  Heads: %d-%d"
              % (pi.mincylinder, pi.maxcylinder, pi.minhead, pi.maxhead))
        return s

    @classmethod
    def from_file(cls, name):

        ipf = cls()

        ipf.iid = ipf.lib.CAPSAddImage()
        error.check(ipf.iid >= 0, "Could not create IPF image container")
        cname = ct.c_char_p(name.encode())
        res = ipf.lib.CAPSLockImage(ipf.iid, cname)
        error.check(res == 0, "Could not open IPF image '%s'" % name)
        res = ipf.lib.CAPSLoadImage(ipf.iid, DI_LOCK.def_flags)
        error.check(res == 0, "Could not load IPF image '%s'" % name)
        ipf.pi = CapsImageInfo()
        res = ipf.lib.CAPSGetImageInfo(ct.byref(ipf.pi), ipf.iid)
        error.check(res == 0, "Could not get info for IPF '%s'" % name)
        print(ipf)

        return ipf


    def get_track(self, cyl, head):
        pi = self.pi
        if head < pi.minhead or head > pi.maxhead:
            return None
        if cyl < pi.mincylinder or cyl > pi.maxcylinder:
            return None

        ti = CapsTrackInfoT2(2)
        res = self.lib.CAPSLockTrack(ct.byref(ti), self.iid,
                                     cyl, head, DI_LOCK.def_flags)
        error.check(res == 0, "Could not lock IPF track %d.%d" % (cyl, head))

        if not ti.trackbuf:
            return None # unformatted/empty
        carray_type = ct.c_ubyte * ((ti.tracklen+7)//8)
        carray = carray_type.from_address(
            ct.addressof(ti.trackbuf.contents))
        trackbuf = bitarray(endian='big')
        trackbuf.frombytes(bytes(carray))
        trackbuf = trackbuf[:ti.tracklen]

        data = []
        for i in range(ti.sectorcnt):
            si = CapsSectorInfo()
            res = self.lib.CAPSGetInfo(ct.byref(si), self.iid,
                                       cyl, head, 1, i)
            error.check(res == 0, "Couldn't get sector info")
            # Adjust the range start to be splice- rather than index-relative
            data.append(((si.datastart - ti.overlap) % ti.tracklen,
                         si.datasize))

        weak = []
        for i in range(ti.weakcnt):
            wi = CapsDataInfo()
            res = self.lib.CAPSGetInfo(ct.byref(wi), self.iid,
                                       cyl, head, 2, i)
            error.check(res == 0, "Couldn't get weak data info")
            # Adjust the range start to be splice- rather than index-relative
            weak.append(((wi.start - ti.overlap) % ti.tracklen, wi.size))

        timebuf = None
        if ti.timebuf:
            carray_type = ct.c_uint * ti.timelen
            carray = carray_type.from_address(
                ct.addressof(ti.timebuf.contents))
            # Unpack the per-byte timing info into per-bitcell
            timebuf = []
            for i in carray:
                for j in range(8):
                    timebuf.append(i)
            # Pad the timing info with normal cell lengths as necessary
            for j in range(len(carray)*8, ti.tracklen):
                timebuf.append(1000)
            # Clip the timing info, if necessary.
            timebuf = timebuf[:ti.tracklen]

        # Rotate the track to start at the splice rather than the index.
        if ti.overlap:
            trackbuf = trackbuf[ti.overlap:] + trackbuf[:ti.overlap]
            if timebuf:
                timebuf = timebuf[ti.overlap:] + timebuf[:ti.overlap]
            
        # We don't really have access to the bitrate. It depends on RPM.
        # So we assume a rotation rate of 300 RPM (5 rev/sec).
        rpm = 300

        track = IPFTrack(
            bits = trackbuf,
            time_per_rev = 60/rpm,
            bit_ticks = timebuf,
            splice = ti.overlap,
            weak = weak)
        track.verify = track
        track.sectors = data
        return track


# Open and initialise the CAPS library.
def open_libcaps():

    # Get the OS-dependent list of valid CAPS library names.
    _names = []
    if platform.system() == "Linux":
        _names = [ "libcapsimage.so.5", "libcapsimage.so.5.1",
                   "libcapsimage.so.4", "libcapsimage.so.4.2",
                   "libcapsimage.so" ]
    elif platform.system() == "Darwin":
        _names = [ "CAPSImage.framework/CAPSImage",
                   "CAPSImg.framework/CAPSImg"]
    elif platform.system() == "Windows":
        _names = [ "CAPSImg_x64.dll", "CAPSImg.dll" ]

    # Get the absolute path to the root Greaseweazle folder.
    path = os.path.dirname(os.path.abspath(__file__))
    for _ in range(3):
        path = os.path.join(path, os.pardir)
    path = os.path.normpath(path)

    # Create a search list of both relative and absolute library names.
    names = []
    for name in _names:
        names.append(name)
        names.append(os.path.join(path, name))

    # Walk the search list, trying to open the CAPS library.
    for name in names:
        try:
            lib = ct.cdll.LoadLibrary(name)
            break
        except:
            pass
    
    error.check("lib" in locals(), """\
Could not find SPS/CAPS IPF decode library
For installation instructions please read the wiki:
<https://github.com/keirf/greaseweazle/wiki/IPF-Images>""")
    
    # We have opened the library. Now initialise it.
    res = lib.CAPSInit()
    error.check(res == 0, "Failure initialising IPF library '%s'" % name)

    return lib


# Get a reference to the CAPS library. Open it if necessary.
def get_libcaps():
    global libcaps
    if not 'libcaps' in globals():
        libcaps = open_libcaps()
    return libcaps


# Local variables:
# python-indent: 4
# End:
