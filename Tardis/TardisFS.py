#! /usr/bin/python
# vim: set et sw=4 sts=4 fileencoding=utf-8:
#
# Tardis: A Backup System
# Copyright 2013-2016, Eric Koldinger, All Rights Reserved.
# kolding@washington.edu
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the copyright holder nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.


import os      # for filesystem modes (O_RDONLY, etc)
import os.path
import errno   # for error number codes (ENOENT, etc)
               # - note: these must be returned as negatives
import sys
import logging
import logging.handlers
import tempfile
import json
import base64
import time
import stat    # for file properties

#import fuse
from fuse import FUSE, FuseOSError, Operations

import Tardis
import Tardis.CacheDir as CacheDir
import Tardis.Regenerator as Regenerator
import Tardis.Util as Util
import Tardis.Cache as Cache
import Tardis.Defaults as Defaults
import Tardis.TardisDB as TardisDB

print(fuse.fuse_python_api)
fuse.fuse_python_api = (0, 2)

_BackupSetInfo = 0
_LastBackupSet = 1
_DirInfo       = 2
_DirContents   = 3
_FileDetails   = 4
_LinkContents  = 5

_infoEnabled    = True

logger = logging.getLogger("Tracer: ")

logLevels = [logging.WARNING, logging.INFO, logging.DEBUG]

def tracer(func):
    @functools.wraps(func)
    def trace(*args, **kwargs):
        if _infoEnabled:
            logger.info("CALL %s:(%s %s)", func.__name__, str(args)[1:-1], str(kwargs)[1:-1])
        try:
            x = func(*args, **kwargs)
            logger.info("COMPLETE %s:(%s %s) => %s", func.__name__, str(args)[1:-1], str(kwargs)[1:-1], str(x)[:32])
            return x
        except Exception as e:
            logger.error("CALL %s:(%s %s)", func.__name__, str(args)[1:-1], str(kwargs)[1:-1])
            logger.error("%s raised exception %s: %s", func.__name__, e.__class__.__name__, str(e))
            #logger.exception(e)
            raise e
    return trace

def getDepth(path):
    """
    Return the depth of a given path, zero-based from root ('/')
    """
    logger.debug("getDepth: %s", path)
    if path ==  '/':
        return 0
    else:
        return path.count('/')

def getParts(path):
    """
    Return the slash-separated parts of a given path as a list
    Namely, the backupset and the path within the set
    """
    if path == '/':
        return [['/']]
    else:
        return path.strip("/").split('/', 1)

class TardisFS(Operations):
    """
    FUSE filesystem to read data from a Tardis Backup Database
    """
    # Disable pylint complaints about "could me a function" and "unused argument" as lots of required FUSE functions
    # just return "read-only FS" status
    # pragma pylint: disable=no-self-use,unused-argument
    backupsets = {}
    dirInfo = {}
    fsencoding = sys.getfilesystemencoding()

    def __init__(self, *args, **kw):
        super(TardisFS, self).__init__(*args, **kw)
        global logger

        try:
            client   = Defaults.getDefault('TARDIS_CLIENT')
            database = Defaults.getDefault('TARDIS_DB')
            dbdir    = Defaults.getDefault('TARDIS_DBDIR') % { 'TARDIS_DB': database }          # HACK
            dbname   = Defaults.getDefault('TARDIS_DBNAME')
            current  = Defaults.getDefault('TARDIS_RECENT_SET')

            # Parameters
            self.database       = database
            self.client         = client
            self.repoint        = False
            self.password       = None
            self.pwfile         = None
            self.pwprog         = None
            self.keys           = None
            self.dbname         = dbname
            self.dbdir          = dbdir
            self.logfile        = None
            self.cachetime      = 60
            self.nocrypt        = False
            self.noauth         = False
            self.current        = current
            self.authenticate   = True
            self.verbose        = 0

            self.crypt          = None

            # logging.basicConfig(level=logging.WARNING)

            self.parser.add_option("--database", '-D',      help="Path to the Tardis database directory")
            self.parser.add_option("--client", '-C',        help="Client to load database for")
            self.parser.add_option("--password", '-P',      help="Password for this archive (use '-o password=' to prompt for password)")
            self.parser.add_option("--keys",                help="Load keys from the specified file")

            self.parser.add_option(mountopt="database",     help="Path to the Tardis database directory")
            self.parser.add_option(mountopt="client",       help="Client to load database for")
            self.parser.add_option(mountopt="password",     help="Password for this archive (use '-o password=' to prompt for password)")
            self.parser.add_option(mountopt="pwfile",       help="Read password for this archive from the file.  Can be a URL (HTTP/HTTPS or FTP)")
            self.parser.add_option(mountopt="pwprog",       help="Use the specified program to generate the password on stdout")
            self.parser.add_option(mountopt="keys",         help="Load keys from the specified file")
            self.parser.add_option(mountopt="repoint",      help="Make absolute links relative to backupset")
            self.parser.add_option(mountopt="dbname",       help="Database Name")
            self.parser.add_option(mountopt="dbdir",        help="Database Directory (if different from data directory")
            self.parser.add_option(mountopt="cachetime",    help="Lifetime of cached elements in seconds")
            self.parser.add_option(mountopt="logfile",      help="Log to file")
            self.parser.add_option(mountopt="verbose",      help="Logging level")
            self.parser.add_option(mountopt='nocrypt',      help="Disable encryption")
            self.parser.add_option(mountopt='noauth',       help="Disable authentication")
            self.parser.add_option(mountopt='current',      help="Name to use for most recent complete backup")

            res = self.parse(values=self, errex=1)

            if self.logfile:
                handler = logging.handlers.WatchedFileHandler(Util.fullPath(self.logfile))
            else:
                handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(levelname)s : %(name)s : %(message)s"))
            logger.addHandler(handler)
            verbose = int(self.verbose)
            if verbose >= len(logLevels):
                verbose = len(logLevels) - 1
            elif verbose < 0:
                verbose = 0

            logger.setLevel(logLevels[verbose])

            self.log = logging.getLogger("TardisFS")

            self.mountpoint = res.mountpoint

            self.name = "TardisFS:<{}/{}>".format(self.database, self.client)

            password = Util.getPassword(self.password, self.pwfile, self.pwprog, prompt="Password for %s: " % (self.client))
            self.password = None

            self.cache      = Cache.Cache(0, float(self.cachetime))
            self.fileCache  = Cache.Cache(0, float(self.cachetime), 'FileCache')

            #if password:
            #    self.crypt = TardisCrypto.TardisCrypto(password, self.client)
            (self.tardis, self.cacheDir, self.crypt) = Util.setupDataConnection(self.database, self.client, password, self.keys, self.dbname, self.dbdir)
            password = None

            # Remove the crypto object if not encyrpting files.
            if self.nocrypt or self.nocrypt is None:
                self.crypt = None

            if self.noauth or self.noauth is None:
                self.authenticate = False

            # Create a regenerator.
            self.regenerator = Regenerator.Regenerator(self.cacheDir, self.tardis, crypt=self.crypt)
            self.files = {}

            # Make sure we can get something
            self.tardis.lastBackupSet()

            # Fuse variables
            self.flags = 0
            self.multithreaded = 0

        except TardisDB.AuthenticationException as e:
            logger.critical("Authentication failed.  Bad Password")
            sys.exit(2)
        except Exception as e:
            logger.exception(e)
            sys.exit(2)

    def __del__(self):
        if self.tardis:
            self.tardis.close()

    def __repr__(self):
        return self.name

    def fsEncodeName(self, name):
        return name

    def getBackupSetInfo(self, b):
        key = (_BackupSetInfo, b)
        info = self.cache.retrieve(key)
        if info:
            return info
        info = self.tardis.getBackupSetInfo(b)
        self.cache.insert(key, info)
        return info

    def lastBackupSet(self, completed):
        key = (_LastBackupSet, completed)
        backupset = self.cache.retrieve(key)
        if backupset:
            return backupset
        backupset = self.tardis.lastBackupSet(completed=completed)
        self.cache.insert(key, backupset)
        return backupset

    def getDirInfo(self, path):
        """ Return the inode and backupset of a directory """
        #self.log.info("getDirInfo: %s", path)
        key = (_DirInfo, path)
        info = self.cache.retrieve(key)
        if info:
            return info

        #self.log.debug("No cache info available for %s", path)
        parts = getParts(path)
        bsInfo = self.getBackupSetInfo(parts[0])
        if len(parts) == 2:
            subpath = parts[1]
            if self.crypt:
                subpath = self.crypt.encryptPath(subpath)
            #fInfo = self.getFileInfoByPath(subpath, bsInfo['backupset'])
            fInfo = self.getFileInfoByPath(path)
            #self.log.info("fInfo %s %s %s", parts[1], "**", str(fInfo))
            info = (bsInfo, fInfo)
        else:
            fInfo = {'inode': 0, 'device': 0, 'dir': 1}
            info = (bsInfo, fInfo)

        if info:
            self.cache.insert(key, info)
        return info

    def getFileInfoByPath(self, path):
        #self.log.info("getFileInfoByPath: %s", path)

        # First, check the cache
        f = self.fileCache.retrieve(path)
        if f:
            #self.log.debug("getFileInfoByPath: %s found in cache", path)
            return f

        # Not in the cache, look things up
        #self.log.debug("File info for %s not in cache", path)
        (head, tail) = os.path.split(path)
        data = self.getDirInfo(head)
        if data:
            bsInfo, dInfo = data
        else:
            return None

        if bsInfo:
            if self.crypt:
                tail = self.crypt.encryptPath(tail)
            #self.log.debug(str(dInfo))
            f = self.tardis.getFileInfoByName(tail, (dInfo['inode'], dInfo['device']), bsInfo['backupset'])
        else:
            parts = getParts(path)
            b = self.getBackupSetInfo(parts[0])
            subpath = parts[1]
            if self.crypt:
                subpath = self.crypt.encryptPath(subpath)
            #self.log.debug("getFileInfoByPath: %s=>%s", parts[1], subpath)
            f = self.tardis.getFileInfoByPath(subpath, b['backupset'])
        # Cache it.
        self.fileCache.insert(path, f)
        # Return it
        return f

    @tracer
    def fsinit(self):
        global _infoEnabled
        _infoEnabled = logger.isEnabledFor(logging.INFO)
        pass

    @tracer
    def getattr(self, path):
        """
        - st_mode (protection bits)
        - st_ino (inode number)
        - st_dev (device)
        - st_nlink (number of hard links)
        - st_uid (user ID of owner)
        - st_gid (group ID of owner)
        - st_size (size of file, in bytes)
        - st_atime (time of most recent access)
        - st_mtime (time of most recent content modification)
        - st_ctime (platform dependent; time of most recent metadata change on Unix,
                    or the time of creation on Windows).
        """

        #self.log.info("CALL getattr: %s",  path)
        path = self.fsEncodeName(path)

        depth = getDepth(path) # depth of path, zero-based from root
        if depth == 0:
            # Fake the root
            target = self.lastBackupSet(False)
            timestamp = float(target['starttime'])
            st = fuse.Stat()
            st.st_mode = stat.S_IFDIR | 0o555
            st.st_ino = 0
            st.st_dev = 0
            st.st_nlink = 32
            st.st_uid = 0
            st.st_gid = 0
            st.st_size = 4096
            st.st_atime = timestamp
            st.st_mtime = timestamp
            st.st_ctime = timestamp
            return st
        elif depth == 1:
            # Root directory contents
            lead = getParts(path)
            st = fuse.Stat()
            if lead[0] == self.current:
                target = self.lastBackupSet(True)
                timestamp = float(target['endtime'])
                st.st_mode = stat.S_IFLNK | 0o755
                st.st_ino = 1
                st.st_dev = 0
                st.st_nlink = 1
                st.st_uid = 0
                st.st_gid = 0
                st.st_size = 4096
                st.st_atime = timestamp
                st.st_mtime = timestamp
                st.st_ctime = timestamp
                return st
            else:
                f = self.getBackupSetInfo(lead[0])
                #self.log.debug("Got backupset info for %s: %s", lead[0], str(f))
                if f:
                    st = fuse.Stat()
                    timestamp = float(f['starttime'])
                    st.st_mode = stat.S_IFDIR | 0o555
                    st.st_ino = int(float(f['starttime']))
                    st.st_dev = 0
                    st.st_nlink = 2
                    st.st_uid = 0
                    st.st_gid = 0
                    st.st_size = 4096
                    st.st_atime = timestamp
                    st.st_mtime = timestamp
                    st.st_ctime = timestamp
                    return st
        else:
            f = self.getFileInfoByPath(path)
            if f:
                st = fuse.Stat()
                st.st_mode = f["mode"]
                st.st_ino = f["inode"]
                st.st_dev = 0
                st.st_nlink = f["nlinks"]
                st.st_uid = f["uid"]
                st.st_gid = f["gid"]
                if f["size"] is not None:
                    st.st_size = int(f["size"])
                elif f["dir"]:
                    st.st_size = 4096       # Arbitrary number
                else:
                    st.st_size = 0
                st.st_atime = f["mtime"]
                st.st_mtime = f["mtime"]
                st.st_ctime = f["ctime"]
                return st
        return -errno.ENOENT


    @tracer
    def getdir(self, _):
        """
        return: [[('file1', 0), ('file2', 0), ... ]]
        """
        #self.log.info('CALL getdir {}'.format(path))
        return -errno.ENOSYS

    @tracer
    def readdir(self, path, offset):
        #self.log.info("CALL readdir %s Offset: %d", path, offset)
        parent = None

        path = self.fsEncodeName(path)

        key = (_DirContents, path)
        dirents = self.cache.retrieve(key)
        if not dirents:
            dirents = [('.', stat.S_IFDIR), ('..', stat.S_IFDIR)]
            depth = getDepth(path)
            if depth == 0:
                dirents.append((self.current, stat.S_IFLNK))
                entries = self.tardis.listBackupSets()
                dirents.extend([(y['name'], stat.S_IFDIR) for y in entries])
            else:
                parts = getParts(path)
                if depth == 1:
                    b = self.getBackupSetInfo(parts[0])
                    entries = self.tardis.readDirectory((0, 0), b['backupset'])
                else:
                    (b, parent) = self.getDirInfo(path)
                    entries = self.tardis.readDirectory((parent["inode"], parent["device"]), b['backupset'])
                #if self.crypt:
                    #entries = self.decryptNames(entries)

                # For each entry, cache it, so a later getattr() call can use it.
                # Get attr will typically be called promptly after a call to
                now = time.time()
                for e in entries:
                    name  = e['name']
                    if self.crypt:
                        name = self.crypt.decryptFilename(name)
                    name = self.fsEncodeName(name)
                    p = os.path.join(path, name)
                    self.fileCache.insert(p, e, now=now)
                    dirents.append((name, e['mode']))
            self.cache.insert(key, dirents)

        #self.log.debug("Direntries: %s", str(dirents))

        # Now, return each entry in the list.
        for e in dirents:
            (name, mode) = e
            #self.log.debug("readdir %s yielding dir entry for %s.  Mode: %s. Type: %s ", path, e, mode, type(mode))
            yield fuse.Direntry(name, type=stat.S_IFMT(mode))

    @tracer
    def mythread ( self ):
        #self.log.info('mythread')
        return -errno.ENOSYS

    @tracer
    def chmod ( self, path, mode ):
        #self.log.info('CALL chmod {} {}'.format(path, oct(mode)))
        return -errno.EROFS

    @tracer
    def chown ( self, path, uid, gid ):
        #self.log.info( 'CALL chown {} {} {}'.format(path, uid, gid))
        return -errno.EROFS

    @tracer
    def fsync ( self, path, isFsyncFile ):
        #self.log.info( 'CALL fsync {} {}'.format(path, isFsyncFile))
        return -errno.EROFS

    @tracer
    def link ( self, targetPath, linkPath ):
        #self.log.info( 'CALL link {} {}'.format(targetPath, linkPath))
        return -errno.EROFS

    @tracer
    def mkdir ( self, path, mode ):
        #self.log.info( 'CALL mkdir {} {}'.format(path, oct(mode)))
        return -errno.EROFS

    @tracer
    def mknod ( self, path, mode, dev ):
        #self.log.info( 'CALL mknod {} {} {}'.format(path, oct(mode), dev))
        return -errno.EROFS

    @tracer
    def open ( self, path, flags ):
        #self.log.info('CALL open {} {})'.format(path, flags))
        path = self.fsEncodeName(path)

        depth = getDepth(path) # depth of path, zero-based from root

        if depth < 2:
            return -errno.ENOENT

        # TODO: Lock this
        if path in self.files:
            self.files[path]["opens"] += 1
            return 0

        parts = getParts(path)
        b = self.getBackupSetInfo(parts[0])
        if b:
            subpath = parts[1]
            if self.crypt:
                subpath = self.crypt.encryptPath(subpath)
            f = self.regenerator.recoverFile(subpath, b['backupset'], nameEncrypted=True, authenticate=self.authenticate)
            if f:
                logger.debug("Opened file %s", path)
                try:
                    f.flush()
                    f.seek(0)
                except (AttributeError, IOError) as e:
                    logger.exception(e)
                    bytesCopied = 0
                    logger.debug("Copying file to tempfile")
                    temp = tempfile.TemporaryFile()
                    chunk = f.read(65536)
                    while chunk:
                        bytesCopied = bytesCopied + len(chunk)
                        temp.write(chunk)
                        chunk = f.read(65536)
                    f.close()
                    logger.debug("Copied %d bytes to tempfile", bytesCopied)
                    temp.flush()
                    temp.seek(0)
                    f = temp

                self.files[path] = {"file": f, "opens": 1}
                logger.debug("Set files[%s] => %s", path, str(self.files[path]))
                return 0
        # Otherwise.....
        return -errno.ENOENT


    @tracer
    def read ( self, path, length, offset ):
        #self.log.info('CALL read {} {} {}'.format(path, length, offset))
        path = self.fsEncodeName(path)
        f = self.files[path]["file"]
        if f:
            f.seek(offset)
            data = f.read(length)
            logger.debug("Actually read %d bytes of %s", len(data), type(data))
            return data
        else:
            logger.warning("No file for path %s", path)
            return -errno.EINVAL

    @tracer
    def readlink ( self, path ):
        #self.log.info('CALL readlink {}'.format(path))
        path = self.fsEncodeName(path)

        key = (_LinkContents, path)
        link = self.cache.retrieve(key)
        if link:
            return link
        if path == '/' + self.current:
            target = self.lastBackupSet(True)
            logger.debug("Path: %s Target: %s %s", path, target['name'], target['backupset'])
            link = str(target['name'])
            self.cache.insert(key, link)
            return link
        elif getDepth(path) > 1:
            parts = getParts(path)
            b = self.getBackupSetInfo(parts[0])
            if b:
                subpath = parts[1]
                if self.crypt:
                    subpath = self.crypt.encryptPath(subpath)
                f = self.regenerator.recoverFile(subpath, b['backupset'], nameEncrypted=True, authenticate=self.authenticate)
                f.flush()
                link = f.readline()
                f.close()
                if self.repoint:
                    if os.path.isabs(link):
                        link = os.path.join(self.mountpoint, parts[0], os.path.relpath(link, "/"))
                self.cache.insert(key, link)
                return link
        return -errno.ENOENT

    @tracer
    def release ( self, path, flags ):
        path = self.fsEncodeName(path)

        if self.files[path]:
            self.files[path]["opens"] -= 1
            if self.files[path]["opens"] == 0:
                self.files[path]["file"].close()
                del self.files[path]
            return 0
        return -errno.EINVAL

    @tracer
    def rename ( self, oldPath, newPath ):
        #self.log.info('CALL rename {} {}'.format(oldPath, newPath))
        return -errno.EROFS

    @tracer
    def rmdir ( self, path ):
        #self.log.info('CALL rmdir {}'.format(path))
        return -errno.EROFS

    @tracer
    def statfs ( self ):
        """ StatFS """
        #self.log.info('CALL statfs: %s', self.path)
        if isinstance(self.cacheDir, CacheDir):
            fs = os.statvfs(self.cacheDir.root)

            st = fuse.Stat()
            st.f_bsize   = fs.f_bsize
            st.f_frsize  = fs.f_frsize
            st.f_blocks  = fs.f_blocks
            st.f_bfree   = fs.f_bfree
            st.f_bavail  = fs.f_bavail
            st.f_files   = fs.f_files
            st.f_ffree   = fs.f_ffree
            st.f_favail  = fs.f_favail
            st.f_flag    = fs.f_flag
            st.f_namemax = fs.f_namemax
            return st
        else:
            return -errno.EINVAL

    def symlink ( self, targetPath, linkPath ):
        #self.log.info('CALL symlink {} {}'.format(path, linkPath))
        return -errno.EROFS

    def truncate ( self, path, size ):
        #self.log.info('CALL truncate {} {}'.format(path, size))
        return -errno.EROFS

    def unlink ( self, path ):
        #self.log.info('CALL unlink {}'.format(path))
        return -errno.EROFS

    def utime ( self, path, times ):
        #self.log.info('CALL utime {} {} '.format(path, str(times)))
        return -errno.EROFS

    def write ( self, path, buf, offset ):
        #self.log.info('CALL write {} {} {}'.format(path, offset, len(buf)))
        return -errno.EROFS

    # Map extrenal attribute names for the top level directories to backupset info names
    attrMap = {
        'user.priority' : 'priority',
        'user.complete' : 'completed',
        'user.backupset': 'backupset',
        'user.session'  : 'session'
    }

    @tracer
    def listxattr ( self, path, size ):
        path = self.fsEncodeName(path)
        self.log.info('CALL listxattr %s %d', path, size)
        if size == 0:
            retFunc = lambda x: len("".join(x)) + len(str(x))
        else:
            retFunc = lambda x: x

        if getDepth(path) == 1:
            parts = getParts(path)
            b = self.getBackupSetInfo(parts[0])
            if b:
                return retFunc(list(self.attrMap.keys()))

        if getDepth(path) > 1:
            parts = getParts(path)
            b = self.getBackupSetInfo(parts[0])
            if b:
                subpath = parts[1]
                if self.crypt:
                    subpath = self.crypt.encryptPath(subpath)
                info = self.tardis.getFileInfoByPath(subpath, b['backupset'])
                if info:
                    attrs = ['user.tardis_checksum', 'user.tardis_since', 'user.tardis_chain']
                    self.log.info("xattrs: %s", info['xattrs'])
                    if info['xattrs']:
                        f = self.regenerator.recoverChecksum(info['xattrs'], authenticate=self.authenticate)
                        xattrs = json.loads(f.read())
                        self.log.debug("Xattrs: %s", str(xattrs))
                        attrs += list(map(str, list(xattrs.keys())))
                        self.log.debug("Adding xattrs: %s", list(xattrs.keys()))
                        self.log.info("Xattrs: %s", str(attrs))
                        self.log.info("Returning: %s", str(retFunc(attrs)))

                    return retFunc(attrs)

        return None

    @tracer
    def getxattr (self, path, attr, size):
        path = self.fsEncodeName(path)
        #self.log.info('CALL getxattr: %s %s %s', path, attr, size)
        if size == 0:
            retFunc = lambda x: len(str(x))
        else:
            retFunc = str

        depth = getDepth(path)

        if depth == 1:
            if attr in self.attrMap:
                parts = getParts(path)
                b = self.getBackupSetInfo(parts[0])
                if self.attrMap[attr] in list(b.keys()):
                    return retFunc(b[self.attrMap[attr]])

        if depth > 1:
            parts = getParts(path)
            b = self.getBackupSetInfo(parts[0])

            subpath = parts[1]
            if self.crypt:
                subpath = self.crypt.encryptPath(subpath)
            if attr == 'user.tardis_checksum':
                if b:
                    checksum = self.tardis.getChecksumByPath(subpath, b['backupset'])
                    #self.log.debug(str(checksum))
                    if checksum:
                        return retFunc(checksum)
            elif attr == 'user.tardis_since':
                if b:
                    since = self.tardis.getFirstBackupSet(subpath, b['backupset'])
                    #self.log.debug(str(since))
                    if since:
                        return retFunc(since)
            elif attr == 'user.tardis_chain':
                info = self.tardis.getChecksumInfoByPath(subpath, b['backupset'])
                #self.log.debug(str(checksum))
                if info:
                    chain = str(info['chainlength'])
                    self.log.debug(str(chain))
                    return retFunc(chain)
            else:
                # Must be an imported value.  Let's generate it.
                info = self.getFileInfoByPath(path)
                if info['xattrs']:
                    f = self.regenerator.recoverChecksum(info['xattrs'], authenticate=self.authenticate)
                    xattrs = json.loads(f.read())
                    if attr in xattrs:
                        value = base64.b64decode(xattrs[attr])
                        return retFunc(value)

        return 0

def main():
    #logging.basicConfig()
    try:
        fs = TardisFS()
    except:
        sys.exit(1)

    fs.flags = 0
    fs.multithreaded = 0

    try:
        logger.debug("TardsFS Version: %s", Tardis.__versionstring__)
        logger.debug("Database: %s", fs.database)
        logger.debug("Client: %s", fs.client)
        logger.debug("MountPoint: %s", fs.mountpoint)
        logger.debug("DBName: %s", fs.dbname)
        logger.debug("Repoint Links: %s", fs.repoint)
        fs.main()
    except Exception as e:
        logger.exception(e)

if __name__ == "__main__":
    main()
