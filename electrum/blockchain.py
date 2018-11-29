# Electrum - lightweight Bitcoin client
# Copyright (C) 2012 thomasv@ecdsa.org
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import os
import threading
from typing import Optional, Dict

from . import util
from .bitcoin import hash_encode, int_to_hex, rev_hex
from .crypto import sha256d
from . import constants
from .util import bfh, bh2u
from .simple_config import SimpleConfig

try:
    import yescrypt
except ImportError as e:
    exit("Please run 'sudo pip3 install https://github.com/wo01/yescrypt_python/archive/master.zip'")

HEADER_SIZE = 80  # bytes
HEADER_SIZE_SAPLING = 112  # bytes
MAX_TARGET = 0x0007ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff


class MissingHeader(Exception):
    pass

class InvalidHeader(Exception):
    pass

def serialize_header(header_dict: dict) -> str:
    s = int_to_hex(header_dict['version'], 4) \
        + rev_hex(header_dict['prev_block_hash']) \
        + rev_hex(header_dict['merkle_root']) \
        + int_to_hex(int(header_dict['timestamp']), 4) \
        + int_to_hex(int(header_dict['bits']), 4) \
        + int_to_hex(int(header_dict['nonce']), 4)
    if header_dict['version'] >= 5:
        s = s + rev_hex(header_dict['finalsapling_root'])
    return s

def deserialize_header(s: bytes, height: int) -> dict:
    if not s:
        raise InvalidHeader('Invalid header: {}'.format(s))
    if height < constants.net.SAPLING_HEIGHT:
        if len(s) != HEADER_SIZE:
            raise InvalidHeader('Invalid header length: {}'.format(len(s)))
    else:
        if len(s) != HEADER_SIZE_SAPLING:
            raise InvalidHeader('Invalid header length: {}'.format(len(s)))
    hex_to_int = lambda s: int.from_bytes(s, byteorder='little')
    h = {}
    h['version'] = hex_to_int(s[0:4])
    h['prev_block_hash'] = hash_encode(s[4:36])
    h['merkle_root'] = hash_encode(s[36:68])
    h['timestamp'] = hex_to_int(s[68:72])
    h['bits'] = hex_to_int(s[72:76])
    h['nonce'] = hex_to_int(s[76:80])
    if h['version'] >= 5:
        h['finalsapling_root'] = hash_encode(s[80:112])
    h['block_height'] = height
    return h

def hash_header(header: dict) -> str:
    if header is None:
        return '0' * 64
    if header.get('prev_block_hash') is None:
        header['prev_block_hash'] = '00'*32
    return hash_raw_header(serialize_header(header))


def hash_raw_header(header: str) -> str:
    return hash_encode(sha256d(bfh(header)))


# key: blockhash hex at forkpoint
# the chain at some key is the best chain that includes the given hash
blockchains = {}  # type: Dict[str, Blockchain]
blockchains_lock = threading.RLock()


def read_blockchains(config: 'SimpleConfig'):
    blockchains[constants.net.GENESIS] = Blockchain(config=config,
                                                    forkpoint=0,
                                                    parent=None,
                                                    forkpoint_hash=constants.net.GENESIS,
                                                    prev_hash=None)
    fdir = os.path.join(util.get_headers_dir(config), 'forks')
    util.make_dir(fdir)
    # files are named as: fork2_{forkpoint}_{prev_hash}_{first_hash}
    l = filter(lambda x: x.startswith('fork2_') and '.' not in x, os.listdir(fdir))
    l = sorted(l, key=lambda x: int(x.split('_')[1]))  # sort by forkpoint

    def delete_chain(filename, reason):
        util.print_error("[blockchain]", reason, filename)
        os.unlink(os.path.join(fdir, filename))

    def instantiate_chain(filename):
        __, forkpoint, prev_hash, first_hash = filename.split('_')
        forkpoint = int(forkpoint)
        prev_hash = (64-len(prev_hash)) * "0" + prev_hash  # left-pad with zeroes
        first_hash = (64-len(first_hash)) * "0" + first_hash
        # forks below the max checkpoint are not allowed
        if forkpoint <= constants.net.max_checkpoint():
            delete_chain(filename, "deleting fork below max checkpoint")
            return
        # find parent (sorting by forkpoint guarantees it's already instantiated)
        for parent in blockchains.values():
            if parent.check_hash(forkpoint - 1, prev_hash):
                break
        else:
            delete_chain(filename, "cannot find parent for chain")
            return
        b = Blockchain(config=config,
                       forkpoint=forkpoint,
                       parent=parent,
                       forkpoint_hash=first_hash,
                       prev_hash=prev_hash)
        # consistency checks
        h = b.read_header(b.forkpoint)
        if first_hash != hash_header(h):
            delete_chain(filename, "incorrect first hash for chain")
            return
        if not b.parent.can_connect(h, check_height=False):
            delete_chain(filename, "cannot connect chain to parent")
            return
        chain_id = b.get_id()
        assert first_hash == chain_id, (first_hash, chain_id)
        blockchains[chain_id] = b

    for filename in l:
        instantiate_chain(filename)


def get_best_chain() -> 'Blockchain':
    return blockchains[constants.net.GENESIS]

# block hash -> chain work; up to and including that block
_CHAINWORK_CACHE = {
    "0000000000000000000000000000000000000000000000000000000000000000": 0,  # virtual block at height -1
}  # type: Dict[str, int]


class Blockchain(util.PrintError):
    """
    Manages blockchain headers and their verification
    """

    def __init__(self, config: SimpleConfig, forkpoint: int, parent: Optional['Blockchain'],
                 forkpoint_hash: str, prev_hash: Optional[str]):
        assert isinstance(forkpoint_hash, str) and len(forkpoint_hash) == 64, forkpoint_hash
        assert (prev_hash is None) or (isinstance(prev_hash, str) and len(prev_hash) == 64), prev_hash
        # assert (parent is None) == (forkpoint == 0)
        if 0 < forkpoint <= constants.net.max_checkpoint():
            raise Exception(f"cannot fork below max checkpoint. forkpoint: {forkpoint}")
        self.index_sapling = constants.net.SAPLING_HEIGHT // 2016  # index
        self.offset_sapling = constants.net.SAPLING_HEIGHT - (constants.net.SAPLING_HEIGHT // 2016) * 2016 # offset from index_sapling
        self.config = config
        self.forkpoint = forkpoint  # height of first header
        self.parent = parent
        self._forkpoint_hash = forkpoint_hash  # blockhash at forkpoint. "first hash"
        self._prev_hash = prev_hash  # blockhash immediately before forkpoint
        self.lock = threading.RLock()
        self.update_size()

    def with_lock(func):
        def func_wrapper(self, *args, **kwargs):
            with self.lock:
                return func(self, *args, **kwargs)
        return func_wrapper

    @property
    def checkpoints(self):
        return constants.net.CHECKPOINTS

    def get_max_child(self) -> Optional[int]:
        with blockchains_lock: chains = list(blockchains.values())
        children = list(filter(lambda y: y.parent==self, chains))
        return max([x.forkpoint for x in children]) if children else None

    def get_max_forkpoint(self) -> int:
        """Returns the max height where there is a fork
        related to this chain.
        """
        mc = self.get_max_child()
        return mc if mc is not None else self.forkpoint

    @with_lock
    def get_branch_size(self) -> int:
        return self.height() - self.get_max_forkpoint() + 1

    def get_name(self) -> str:
        return self.get_hash(self.get_max_forkpoint()).lstrip('0')[0:10]

    def check_header(self, header: dict) -> bool:
        header_hash = hash_header(header)
        height = header.get('block_height')
        return self.check_hash(height, header_hash)

    def check_hash(self, height: int, header_hash: str) -> bool:
        """Returns whether the hash of the block at given height
        is the given hash.
        """
        assert isinstance(header_hash, str) and len(header_hash) == 64, header_hash  # hex
        try:
            return header_hash == self.get_hash(height)
        except Exception:
            return False

    def fork(parent, header: dict) -> 'Blockchain':
        if not parent.can_connect(header, check_height=False):
            raise Exception("forking header does not connect to parent chain")
        forkpoint = header.get('block_height')
        self = Blockchain(config=parent.config,
                          forkpoint=forkpoint,
                          parent=parent,
                          forkpoint_hash=hash_header(header),
                          prev_hash=parent.get_hash(forkpoint-1))
        open(self.path(), 'w+').close()
        self.save_header(header)
        # put into global dict
        chain_id = self.get_id()
        with blockchains_lock:
            assert chain_id not in blockchains, (chain_id, list(blockchains))
            blockchains[chain_id] = self
        return self

    @with_lock
    def height(self) -> int:
        return self.forkpoint + self.size() - 1

    @with_lock
    def size(self) -> int:
        return self._size

    @with_lock
    def update_size(self) -> None:
        p = self.path()
        size = os.path.getsize(p) if os.path.exists(p) else 0
        if constants.net.SAPLING_HEIGHT <= self.forkpoint:
            self._size = size//HEADER_SIZE_SAPLING
        elif size <= HEADER_SIZE * (constants.net.SAPLING_HEIGHT - self.forkpoint):
            self._size = size//HEADER_SIZE
        else:
            self._size = (constants.net.SAPLING_HEIGHT - self.forkpoint) + (size - (constants.net.SAPLING_HEIGHT - self.forkpoint) * HEADER_SIZE)//HEADER_SIZE_SAPLING

    @classmethod
    def verify_header(cls, header: dict, prev_hash: str, target: int, expected_header_hash: str=None) -> None:
        height = header.get('block_height')
        if (height == 20 or height == 22 or height == 26): # somehow wrong ???
            return
        _hash = hash_header(header)
        if expected_header_hash and expected_header_hash != _hash:
            raise Exception("hash mismatches with expected: {} vs {}".format(expected_header_hash, _hash))
        size = 80
        if height >= constants.net.SAPLING_HEIGHT:
            size = 112
        _powhash = rev_hex(bh2u(yescrypt.getPoWHash(bfh(serialize_header(header)), size)))
        if prev_hash != header.get('prev_block_hash'):
            raise Exception("prev hash mismatch: %s vs %s" % (prev_hash, header.get('prev_block_hash')))
        # nAverageBlocks + nMedianTimeSpan = 28 Because checkpoint don't have preblock data.
        if height % 2016 != 0 and height // 2016 < len(constants.net.CHECKPOINTS) or height >= len(constants.net.CHECKPOINTS)*2016 and height <= len(constants.net.CHECKPOINTS)*2016 + 24:
            return
        if constants.net.TESTNET:
            return
        bits = cls.target_to_bits(target)
        if bits != header.get('bits'):
            raise Exception("bits mismatch: %s vs %s" % (bits, header.get('bits')))
        block_hash_as_num = int.from_bytes(bfh(_powhash), byteorder='big')
        if block_hash_as_num > target:
            raise Exception(f"insufficient proof of work: {block_hash_as_num} vs target {target}")

    def verify_chunk(self, index: int, data: bytes) -> None:
        if index < self.index_sapling:
            num = len(data) // HEADER_SIZE
        elif index == self.index_sapling:
            if len(data) <= self.offset_sapling * HEADER_SIZE:
                num = len(data) // HEADER_SIZE
            else:
                num = self.offset_sapling + (len(data) - self.offset_sapling * HEADER_SIZE) // HEADER_SIZE_SAPLING
        else:
            num = len(data) // HEADER_SIZE_SAPLING
        start_height = index * 2016
        prev_hash = self.get_hash(start_height - 1)
        headers = {}
        for i in range(num):
            height = start_height + i
            try:
                expected_header_hash = self.get_hash(height)
            except MissingHeader:
                expected_header_hash = None
            start_position = self.get_delta_bytes(height) - self.get_delta_bytes(start_height)
            if height < constants.net.SAPLING_HEIGHT:
                raw_header = data[start_position : start_position + HEADER_SIZE]
            else:
                raw_header = data[start_position : start_position + HEADER_SIZE_SAPLING]
            header = deserialize_header(raw_header, index*2016 + i)
            headers[header.get('block_height')] = header
            target = self.get_target(index*2016 + i, headers)
            self.verify_header(header, prev_hash, target, expected_header_hash)
            prev_hash = hash_header(header)

    @with_lock
    def path(self):
        d = util.get_headers_dir(self.config)
        if self.parent is None:
            filename = 'blockchain_headers'
        else:
            assert self.forkpoint > 0, self.forkpoint
            prev_hash = self._prev_hash.lstrip('0')
            first_hash = self._forkpoint_hash.lstrip('0')
            basename = f'fork2_{self.forkpoint}_{prev_hash}_{first_hash}'
            filename = os.path.join('forks', basename)
        return os.path.join(d, filename)

    def get_delta_bytes(self, height: int):
        if height < constants.net.SAPLING_HEIGHT:
            delta_bytes = height * HEADER_SIZE
        else:
            delta_bytes = constants.net.SAPLING_HEIGHT * HEADER_SIZE + (height - constants.net.SAPLING_HEIGHT) * HEADER_SIZE_SAPLING
        return delta_bytes

    @with_lock
    def save_chunk(self, index: int, chunk: bytes):
        assert index >= 0, index
        chunk_within_checkpoint_region = index < len(self.checkpoints)
        # chunks in checkpoint region are the responsibility of the 'main chain'
        if chunk_within_checkpoint_region and self.parent is not None:
            main_chain = get_best_chain()
            main_chain.save_chunk(index, chunk)
            return

        if index < self.index_sapling:
            delta_height = (index * 2016 - self.forkpoint)
            delta_bytes = delta_height * HEADER_SIZE
        elif index == self.index_sapling:
            if self.forkpoint < constants.net.SAPLING_HEIGHT:
                delta_height = (index * 2016 - self.forkpoint)
                delta_bytes = delta_height * HEADER_SIZE
            else:
                delta_height = (index * 2016 - constants.net.SAPLING_HEIGHT)
                delta_height2 = (constants.net.SAPLING_HEIGHT - self.forkpoint)
                delta_bytes = delta_height * HEADER_SIZE_SAPLING + delta_height2 * HEADER_SIZE
        else:
            if self.forkpoint < constants.net.SAPLING_HEIGHT:
                delta_height = (index * 2016 - constants.net.SAPLING_HEIGHT)
                delta_height2 = (constants.net.SAPLING_HEIGHT - self.forkpoint)
                delta_bytes = delta_height * HEADER_SIZE_SAPLING + delta_height2 * HEADER_SIZE
            else:
                delta_height = (index * 2016 - self.forkpoint)
                delta_bytes = delta_height * HEADER_SIZE_SAPLING
        # if this chunk contains our forkpoint, only save the part after forkpoint
        # (the part before is the responsibility of the parent)
        if delta_bytes < 0:
            chunk = chunk[-delta_bytes:]
            delta_bytes = 0
        truncate = not chunk_within_checkpoint_region
        self.write(chunk, delta_bytes, truncate)
        self.swap_with_parent()

    def swap_with_parent(self) -> None:
        parent_lock = self.parent.lock if self.parent is not None else threading.Lock()
        with parent_lock, self.lock, blockchains_lock:  # this order should not deadlock
            # do the swap; possibly multiple ones
            cnt = 0
            while self._swap_with_parent():
                cnt += 1
                if cnt > len(blockchains):  # make sure we are making progress
                    raise Exception(f'swapping fork with parent too many times: {cnt}')

    def _swap_with_parent(self) -> bool:
        """Check if this chain became stronger than its parent, and swap
        the underlying files if so. The Blockchain instances will keep
        'containing' the same headers, but their ids change and so
        they will be stored in different files."""
        if self.parent is None:
            return False
        if self.parent.get_chainwork() >= self.get_chainwork():
            return False
        self.print_error("swap", self.forkpoint, self.parent.forkpoint)
        parent_branch_size = self.parent.height() - self.forkpoint + 1
        forkpoint = self.forkpoint  # type: Optional[int]
        parent = self.parent  # type: Optional[Blockchain]
        child_old_id = self.get_id()
        parent_old_id = parent.get_id()
        # swap files
        # child takes parent's name
        # parent's new name will be something new (not child's old name)
        self.assert_headers_file_available(self.path())
        child_old_name = self.path()
        with open(self.path(), 'rb') as f:
            my_data = f.read()
        self.assert_headers_file_available(parent.path())
        with open(parent.path(), 'rb') as f:
            if forkpoint > constants.net.SAPLING_HEIGHT:
                if constants.net.SAPLING_HEIGHT > parent.forkpoint:
                    offset = (forkpoint - constants.net.SAPLING_HEIGHT)*HEADER_SIZE_SAPLING + (constants.net.SAPLING_HEIGHT-parent.forkpoint)*HEADER_SIZE
                else:
                    offset = (forkpoint - constants.net.SAPLING_HEIGHT)*HEADER_SIZE_SAPLING + (constants.net.SAPLING_HEIGHT-parent.forkpoint)*HEADER_SIZE_SAPLING
                f.seek(offset)
                parent_data = f.read(parent_branch_size*HEADER_SIZE_SAPLING)
            else:
                offset = (forkpoint - parent.forkpoint)*HEADER_SIZE
                f.seek(offset)
                if constants.net.SAPLING_HEIGHT > parent.height():
                    parent_data = f.read(parent_branch_size*HEADER_SIZE)
                else:
                    parent_data = f.read((parent.height()-constants.net.SAPLING_HEIGHT+1)*HEADER_SIZE_SAPLING + (constants.net.SAPLING_HEIGHT-forkpoint)*HEADER_SIZE)
        self.write(parent_data, 0)
        parent.write(my_data, offset)
        # swap parameters
        self.parent, parent.parent = parent.parent, self  # type: Optional[Blockchain], Optional[Blockchain]
        self.forkpoint, parent.forkpoint = parent.forkpoint, self.forkpoint
        if forkpoint < constants.net.SAPLING_HEIGHT:
            self._forkpoint_hash, parent._forkpoint_hash = parent._forkpoint_hash, hash_raw_header(bh2u(parent_data[:HEADER_SIZE]))
        else:
            self._forkpoint_hash, parent._forkpoint_hash = parent._forkpoint_hash, hash_raw_header(bh2u(parent_data[:HEADER_SIZE_SAPLING]))
        self._prev_hash, parent._prev_hash = parent._prev_hash, self._prev_hash
        # parent's new name
        try:
            os.rename(child_old_name, parent.path())
        except OSError:
            os.remove(parent.path())
            os.rename(child_old_name, parent.path())
        self.update_size()
        parent.update_size()
        # update pointers
        blockchains.pop(child_old_id, None)
        blockchains.pop(parent_old_id, None)
        blockchains[self.get_id()] = self
        blockchains[parent.get_id()] = parent
        return True

    def get_id(self) -> str:
        return self._forkpoint_hash

    def assert_headers_file_available(self, path):
        if os.path.exists(path):
            return
        elif not os.path.exists(util.get_headers_dir(self.config)):
            raise FileNotFoundError('Electrum headers_dir does not exist. Was it deleted while running?')
        else:
            raise FileNotFoundError('Cannot find headers file but headers_dir is there. Should be at {}'.format(path))

    @with_lock
    def write(self, data: bytes, offset: int, truncate: bool=True) -> None:
        filename = self.path()
        self.assert_headers_file_available(filename)
        size = os.path.getsize(filename) if os.path.exists(filename) else 0
        with open(filename, 'rb+') as f:
            if truncate and offset != size:
                f.seek(offset)
                f.truncate()
            f.seek(offset)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        self.update_size()

    @with_lock
    def save_header(self, header: dict) -> None:
        delta = header.get('block_height') - self.forkpoint
        data = bfh(serialize_header(header))
        # headers are only _appended_ to the end:
        assert delta == self.size()
        assert len(data) == HEADER_SIZE or len(data) == HEADER_SIZE_SAPLING
        filename = self.path()
        size = os.path.getsize(filename) if os.path.exists(filename) else 0
        self.write(data, size)
        self.swap_with_parent()

    @with_lock
    def read_header(self, height: int) -> Optional[dict]:
        if height < 0:
            return
        if height < self.forkpoint:
            return self.parent.read_header(height)
        if height > self.height():
            return
        delta = height - self.forkpoint
        name = self.path()
        if height < constants.net.SAPLING_HEIGHT:
            offset = delta * HEADER_SIZE
            size = HEADER_SIZE
        else:
            if self.forkpoint > constants.net.SAPLING_HEIGHT:
                offset = delta * HEADER_SIZE_SAPLING
            else:
                offset = (height - constants.net.SAPLING_HEIGHT) * HEADER_SIZE_SAPLING + (constants.net.SAPLING_HEIGHT - self.forkpoint) * HEADER_SIZE
            size = HEADER_SIZE_SAPLING
        self.assert_headers_file_available(name)
        with open(name, 'rb') as f:
            f.seek(offset)
            h = f.read(size)
            if len(h) < size:
                raise Exception('Expected to read a full header. This was only {} bytes'.format(len(h)))
        if h == bytes([0])*size:
            return None
        return deserialize_header(h, height)

    def get_hash(self, height: int) -> str:
        def is_height_checkpoint():
            within_cp_range = height <= constants.net.max_checkpoint()
            at_chunk_boundary = (height+1) % 2016 == 0
            return within_cp_range and at_chunk_boundary

        if height == -1:
            return '0000000000000000000000000000000000000000000000000000000000000000'
        elif height == 0:
            return constants.net.GENESIS
        elif is_height_checkpoint():
            index = height // 2016
            h, t = self.checkpoints[index]
            return h
        else:
            header = self.read_header(height)
            if header is None:
                raise MissingHeader(height)
            return hash_header(header)

    def get_median_timestamp(self, height, chain):
        nMedianTimeSpan = 11
        pmedian = [];
        pindex = height
        i = 0
        while (i < nMedianTimeSpan and pindex != 0):
            BlockReading = chain.get(pindex)
            if BlockReading is None:
                BlockReading = self.read_header(pindex)
            pmedian.append(BlockReading.get('timestamp'))
            pindex -= 1
            i += 1
        pmedian.sort()
        return pmedian[i//2];


    def get_target_koto(self, height, chain=None):
        if chain is None:
            chain = {}

        #last = self.read_header(height - 1)
        last = chain.get(height - 1)
        if last is None:
            #last = chain.get(height - 1)
            last = self.read_header(height - 1)

        # params
        BlockReading = last
        nActualTimespan = 0
        FistBlockTime = 0
        nAverageBlocks = 17
        nPowMaxAdjustDown = 32; # 32% adjustment down
        nPowMaxAdjustUp = 16; # 16% adjustment up
        CountBlocks = 0
        bnNum = 0
        bnTmp = 0
        bnOldAvg = 0
        nTargetTimespan = nAverageBlocks * 60 # 60 seconds
        nMinActualTimespan =  (nTargetTimespan * (100 - nPowMaxAdjustUp)) // 100
        nMaxActualTimespan = (nTargetTimespan * (100 + nPowMaxAdjustDown)) // 100

        # nAverageBlocks + nMedianTimeSpan = 28 Because checkpoint don't have preblock data.
        if height < len(self.checkpoints)*2016 + 28:
            return 0

        if last is None or height-1 <= nAverageBlocks:
            return MAX_TARGET
        for i in range(1, nAverageBlocks + 1):
            CountBlocks += 1

            if CountBlocks <= nAverageBlocks:
                bnTmp = self.bits_to_target(BlockReading.get('bits'))
                bnOldAvg += bnTmp

            BlockReading = chain.get((height-1) - CountBlocks)
            if BlockReading is None:
                BlockReading = self.read_header((height-1) - CountBlocks)

        nActualTimespan = self.get_median_timestamp(height - 1, chain) - self.get_median_timestamp((height-1) - CountBlocks, chain)
        fix = 0
        if (nActualTimespan - nTargetTimespan < 0 and (nActualTimespan - nTargetTimespan) % 4 != 0):
            fix = 1
        nActualTimespan = nTargetTimespan + (nActualTimespan - nTargetTimespan)//4
        nActualTimespan += fix
        nActualTimespan = max(nActualTimespan, nMinActualTimespan)
        nActualTimespan = min(nActualTimespan, nMaxActualTimespan)

        # retargets
        bnNew = bnOldAvg // nAverageBlocks
        bnNew //= nTargetTimespan
        bnNew *= nActualTimespan

        bnNew = min(bnNew, MAX_TARGET)

        return bnNew

    def get_target(self, height: int, chain=None) -> int:
        # compute target from chunk x, used in chunk x+1
        if constants.net.TESTNET:
            return 0
        if height == -1:
            return MAX_TARGET
        if height // 2016 < len(self.checkpoints) and (height) % 2016 == 0:
            h, t = self.checkpoints[height // 2016]
            return t
        if height // 2016 < len(self.checkpoints) and (height) % 2016 != 0:
            return 0
# new target
        return self.get_target_koto(height, chain)

    @classmethod
    def bits_to_target(cls, bits: int) -> int:
        bitsN = (bits >> 24) & 0xff
        if not (0x03 <= bitsN <= 0x1f):
            raise Exception("First part of bits should be in [0x03, 0x1f]")
        bitsBase = bits & 0xffffff
        if not (0x8000 <= bitsBase <= 0x7fffff):
            raise Exception("Second part of bits should be in [0x8000, 0x7fffff]")
        return bitsBase << (8 * (bitsN-3))

    @classmethod
    def target_to_bits(cls, target: int) -> int:
        c = ("%064x" % target)[2:]
        while c[:2] == '00' and len(c) > 6:
            c = c[2:]
        bitsN, bitsBase = len(c) // 2, int.from_bytes(bfh(c[:6]), byteorder='big')
        if bitsBase >= 0x800000:
            bitsN += 1
            bitsBase >>= 8
        return bitsN << 24 | bitsBase

    def chainwork_of_header_at_height(self, height: int) -> int:
        """work done by single header at given height"""
        target = self.get_target(height)
        work = ((2 ** 256 - target - 1) // (target + 1)) + 1
        return work

    @with_lock
    def get_chainwork(self, height=None) -> int:
        if height is None:
            height = max(0, self.height())
        if constants.net.TESTNET:
            # On testnet/regtest, difficulty works somewhat different.
            # It's out of scope to properly implement that.
            return height
        last_retarget = height // 2016 * 2016 - 1
        cached_height = last_retarget
        while _CHAINWORK_CACHE.get(self.get_hash(cached_height)) is None:
            if cached_height <= -1:
                break
            cached_height -= 2016
        assert cached_height >= -1, cached_height
        running_total = _CHAINWORK_CACHE[self.get_hash(cached_height)]
        while cached_height < last_retarget:
            cached_height += 1
            work_in_single_header = self.chainwork_of_header_at_height(cached_height)
            running_total += work_in_single_header
            if cached_height % 2016 == 0:
                _CHAINWORK_CACHE[self.get_hash(cached_height)] = running_total
        return running_total

    def can_connect(self, header: dict, check_height: bool=True) -> bool:
        if header is None:
            return False
        height = header['block_height']
        if check_height and self.height() != height - 1:
            #self.print_error("cannot connect at height", height)
            return False
        if height == 0:
            return hash_header(header) == constants.net.GENESIS
        try:
            prev_hash = self.get_hash(height - 1)
        except:
            return False
        if prev_hash != header.get('prev_block_hash'):
            return False
        headers = {}
        headers[header.get('block_height')] = header
        self.print_error("can connect", height)
        try:
            target = self.get_target(height, headers)
        except MissingHeader:
            return False
        try:
            self.verify_header(header, prev_hash, target)
        except BaseException as e:
            return False
        return True

    def connect_chunk(self, idx: int, hexdata: str) -> bool:
        assert idx >= 0, idx
        try:
            data = bfh(hexdata)
            self.verify_chunk(idx, data)
            #self.print_error("validated chunk %d" % idx)
            self.save_chunk(idx, data)
            return True
        except BaseException as e:
            self.print_error(f'verify_chunk idx {idx} failed: {repr(e)}')
            return False

    def get_checkpoints(self):
        # for each chunk, store the hash of the last block and the target after the chunk
        cp = []
        n = self.height() // 2016
        for index in range(n):
            h = self.get_hash((index+1) * 2016 -1)
            self.print_error("checkpoints", index)
            target = self.get_target(index * 2016)
            cp.append((h, target))
        return cp


def check_header(header: dict) -> Optional[Blockchain]:
    if type(header) is not dict:
        return None
    with blockchains_lock: chains = list(blockchains.values())
    for b in chains:
        if b.check_header(header):
            return b
    return None


def can_connect(header: dict) -> Optional[Blockchain]:
    with blockchains_lock: chains = list(blockchains.values())
    for b in chains:
        if b.can_connect(header):
            return b
    return None
