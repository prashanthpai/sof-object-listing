#!/usr/bin/python

import os
import re
import sys
import time
import libgfchangelog
import xattr

from swift.common.utils import split_path

cl = libgfchangelog.Changes()

# Changelog entry indices
IDX_START = 0
IDX_END = 2
POS_GFID = 0
POS_TYPE = 1
TYPE_ENTRY = "E "

MOUNT_PATH = '/mnt/testvol'
GFID_XATTR = 'glusterfs.ancestry.path'
GFID_DIR = ".gfid"
RESELLER_PREFIX = 'AUTH_'


def created_by_sof(path):
    account, container, obj = split_path(path, 1, 3, True)
    # Dor prefix-dot-suffix SOF file naming convention
    exp = re.compile('^\..*\.[a-z0-9]{32}$')
    if exp.match(obj):
        return True
    return False


def path_is_object(path):
    account, container, obj = split_path(path, 1, 3, True)
    if account and container and obj:
        if account.startswith(RESELLER_PREFIX):
            print "Object: %s" % (obj)
            return True
    return False


def gfid_to_path(gfid):
    file_path = os.path.join(MOUNT_PATH, GFID_DIR, gfid)
    try:
        path = xattr.getxattr(file_path, GFID_XATTR)
        return path
    except IOError:
        print "%s xattr not set on file %s. Check if you have turned on " \
            "'build-pgfid' option in volume." % (GFID_XATTR, file_path)
        return None


def process_change_file(file):

    try:
        f = open(file, "r")
        clist = f.readlines()
        f.close()
    except IOError:
        raise

    for e in clist:
        e = e.strip()
        et = e[IDX_START:IDX_END]    # entry type
        ec = e[IDX_END:].split(' ')  # rest of the bits

        # We only care about creation and deletions.
        # In changelog terminology, this is an ENTRY operation.
        if et == TYPE_ENTRY:
            # Type of entry operation
            ty = ec[POS_TYPE]
            # We deal only with files, for now.
            # UNLINK change is of no use to us as we cannot get path
            # from gfid for a deleted file.
            if ty in ('CREATE'):
                # GFID of the entry
                gfid = ec[POS_GFID]
                path = gfid_to_path(gfid)
                print "%s %s %s" % (ty, gfid, path)
                if path and path_is_object(path) and \
                        not created_by_sof(path):
                    # Update container DB directly or do a pickle dump
                    # to be later picked up by object-updater
                    return


def get_changes(brick, scratch_dir, log_file, log_level, interval):
    change_list = []
    try:
        cl.cl_register(brick, scratch_dir, log_file, log_level)
        while True:
            cl.cl_scan()
            change_list = cl.cl_getchanges()
            for change in change_list:
                process_change_file(change)
                cl.cl_done(change)
            time.sleep(interval)
    except OSError:
        ex = sys.exc_info()[1]
        print ex

if __name__ == '__main__':
    if len(sys.argv) != 5:
        print("usage: %s <brick> <scratch-dir> <log-file> <fetch-interval>"
              % (sys.argv[0]))
        sys.exit(1)
    get_changes(sys.argv[1], sys.argv[2], sys.argv[3], 9, int(sys.argv[4]))
