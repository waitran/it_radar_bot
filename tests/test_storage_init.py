"""Prepare existing volumes without changing data or following external links."""
import os
from pathlib import Path
import tempfile
import unittest
from app.storage_init import prepare_storage
from app.db import Database


class StorageInitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'data';self.root.mkdir()
        self.uid=max(1,os.getuid());self.gid=max(1,os.getgid())

    def test_idempotent_permissions_preserve_file_bytes_and_inode(self):
        nested=self.root/'old';nested.mkdir();path=nested/'state.db'
        path.write_bytes(b'previous data');before=path.stat().st_ino
        for _ in range(2):prepare_storage([self.root],self.uid,self.gid)
        self.assertEqual(path.read_bytes(),b'previous data');self.assertEqual(path.stat().st_ino,before)
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(nested.stat().st_mode & 0o777,0o700)
        self.assertEqual(path.stat().st_uid,self.uid)

    def test_symlink_is_never_followed(self):
        outside=Path(self.tmp.name)/'outside';outside.write_text('untouched')
        before=outside.stat();(self.root/'link').symlink_to(outside)
        with self.assertRaises((OSError,ValueError)):prepare_storage([self.root],self.uid,self.gid)
        self.assertEqual(outside.read_text(),'untouched')
        self.assertEqual((outside.stat().st_mode,outside.stat().st_uid),(before.st_mode,before.st_uid))

    def test_hardlink_is_never_reowned(self):
        outside=Path(self.tmp.name)/'outside';outside.write_text('untouched')
        before=outside.stat();os.link(outside,self.root/'link')
        with self.assertRaises(ValueError):prepare_storage([self.root],self.uid,self.gid)
        self.assertEqual((outside.stat().st_mode,outside.stat().st_uid),(before.st_mode,before.st_uid))

    def test_root_or_invalid_uid_is_rejected(self):
        for value in (0,-1,'10001',True):
            with self.assertRaises(ValueError):prepare_storage([self.root],value,self.gid)

    def test_sqlite_and_lock_are_private_when_created(self):
        path=self.root/'state.db';db=Database(str(path))
        try:
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(Path(str(path)+'.lock').stat().st_mode & 0o777,0o600)
        finally:db.close()
