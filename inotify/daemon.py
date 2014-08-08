#!/usr/bin/env python

import hashlib
import os
import pika
import sys
import time

from eventlet import Timeout

from swift.common.ring import Ring
from swift.common.bufferedhttp import http_connect
from swift.common.exceptions import ConnectionTimeout
from swift.common.utils import generate_trans_id, \
    normalize_timestamp, split_path

DISK_READ_CHUNK_SIZE = 65536
DEVICE_PATH = '/mnt/brick-test/b/'
SP_INDEX = 2


def get_container_ring():
    return Ring('/etc/swift', ring_name='container')


def process_object_update(update):

    part, nodes = get_container_ring().get_nodes(
        update['account'], update['container'])
    obj = '/%s/%s/%s' % \
          (update['account'], update['container'], update['obj'])
    for node in nodes:
            headers = update['headers'].copy()
            object_update(node, part, update['op'], obj,
                          headers)


def object_update(node, part, op, obj, headers):

    headers_out = headers.copy()
    headers_out['user-agent'] = 'obj-updater %s' % os.getpid()
    try:
        with ConnectionTimeout(0.5):
            conn = http_connect(node['ip'], node['port'], node['device'],
                                part, op, obj, headers_out)
        with Timeout(10):
            resp = conn.getresponse()
            resp.read()
            return resp.status
    except (Exception, Timeout):
        raise


def compute_etag(fd):
    etag = hashlib.md5()
    while True:
        chunk = os.read(fd, DISK_READ_CHUNK_SIZE)
        if chunk:
            etag.update(chunk)
        else:
            break
    return etag.hexdigest()


def update_container(op, obj_path, device_path):

    account, container, obj = split_path(obj_path, 3, 3, True)

    headers_out = {}

    if op == 'PUT':
#        try:
#            file_path = os.path.join(device_path, obj_path[1:])
#            fd = os.open(file_path, os.O_RDONLY)
#        except OSError:
#            return
#        stat = os.fstat(fd)
#        headers_out['X-Size'] = str(stat.st_size)  # Not accurate
        headers_out['X-Size'] = 1
        headers_out['X-Timestamp'] = normalize_timestamp(time.time())
#        headers_out['X-Etag'] = str(compute_etag(fd))
        # Don't compute as the file could still be open for writing
        headers_out['X-Etag'] = str('0'*32)
#        os.close(fd)
        headers_out['X-Content-Type'] = 'application/octet-stream'
    elif op == 'DELETE':
        headers_out['X-Timestamp'] = normalize_timestamp(time.time())

    headers_out['X-Trans-Id'] = generate_trans_id('')
    headers_out['User-Agent'] = 'SwiftOnFile watcher'
    headers_out['X-Backend-Storage-Policy-Index'] = str(SP_INDEX)

    data = {'op': op, 'account': account, 'container': container,
            'obj': obj, 'headers': headers_out}

    return data


def callback(channel, method, headers, body):
    print("GOT: %r" % (body,))
    if body.startswith("PUT"):
        path = body[(len('PUT')+1):]
        op = 'PUT'
    elif body.startswith("DELETE"):
        path = body[(len('DELETE')+1):]
        op = 'DELETE'
    else:
        return

    data = update_container(op, path, DEVICE_PATH)
    process_object_update(data)


def main():

    global DEVICE_PATH
    global SP_INDEX

    if len(sys.argv) != 3:
        print("Usage: %s <mountpoint> <sp-index>" % (sys.argv[0]))
        sys.exit(1)

    if os.path.isdir(sys.argv[1]):
        DEVICE_PATH = sys.argv[1]
    else:
        print("%s is not a directory" % (sys.argv[1]))
        sys.exit(1)

    SP_INDEX = sys.argv[2]

    connection = pika.BlockingConnection(pika.ConnectionParameters(
                                         host='localhost'))
    channel = connection.channel()
    channel.queue_declare(queue='sof')
    channel.basic_consume(callback,
                          queue='sof',
                          no_ack=True)

    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        print("Keyboard Interrupt. Exiting gracefully")
        channel.stop_consuming()
    connection.close()


if __name__ == '__main__':
    main()
