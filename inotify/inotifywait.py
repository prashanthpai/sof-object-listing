#!/usr/bin/env python

import os
import pika
import re
import shlex
import signal
import subprocess
import sys
import xattr


class EventsQueue(object):

    def __init__(self, host='localhost', name='sof'):
        self.host = host
        self.name = name

        self.connection = pika.BlockingConnection(pika.ConnectionParameters(
                                                  host=self.host))
        self.channel = self.connection.channel()
        self.channel.queue_declare(queue=self.name)

    def enqueue(self, message):
        self.channel.basic_publish(exchange='',
                                   routing_key=self.name,
                                   body=message)
        print("SENT: %s" % (message))

    def close(self):
        self.channel.close()
        self.connection.close()


def op_by_sof(op, path):

    if op == 'PUT':
        # Check if this is temp file created by swiftonfile having
        # dot prefix dot suffix notation
        exp = re.compile('^\..*\.[a-z0-9]{32}$')
        if exp.match(os.path.basename(path)):
            return True

        # Check if Swift xattr exist
        try:
            xattr.getxattr(path, 'user.swift.metadata')
            return True
        except IOError:
            pass

        return False

    elif op == 'DELETE':
        # SOF renames files to .ts before deletion
        return path.strip().endswith('.ts')


def parse_inotifywait_line(line, device_path, queue):

    if line.startswith("CLOSE_WRITE:CLOSE"):
        path = line[(len("CLOSE_WRITE:CLOSE")+1):]
        op = 'PUT'
    elif line.startswith("CREATE:ISDIR"):
        return
    elif line.startswith("DELETE:ISDIR"):
        # Ignore directory removal
        return
    elif line.startswith("CREATE"):
        path = line[(len("CREATE")+1):]
        op = 'PUT'
    elif line.startswith("DELETE"):
        path = line[(len("DELETE")+1):]
        op = 'DELETE'

    # Remove device_path prefix
    path = path[len(device_path):]

    if path.startswith('/async_pending') or \
            path.startswith('/.glusterfs'):
        return

    # For a file to be an object, it must be at least two levels
    # down the directory tree
    if len(path.split('/')) <= 3:
        return

    # Check if file creation/deletion was done by SOF itself, if yes, ignore.
    if op_by_sof(op, os.path.join(device_path, path)):
        return

    payload = ("%s %s" % (op, path)).strip()
    queue.enqueue(payload)


def main():

    if len(sys.argv) != 2:
        print("Usage: %s <dir/mountpoint>" % (sys.argv[0]))
        sys.exit(1)

    if os.path.isdir(sys.argv[1]):
        dirpath = sys.argv[1]
    else:
        print("%s is not a directory" % (sys.argv[1]))
        sys.exit(1)

    # Create an event queue using RabbitMQ
    queue = EventsQueue()

    # Using inotifywait as it is a simple recursive implementation
    # watchdog does not expose IN_CLOSE_WRITE whereas pyinotify is not
    # recursive in realtime
    args = shlex.split("inotifywait -rm -e create,delete --format '%:e %w%f' "
                       "--exclude=.glusterfs")
    args.append(dirpath)

    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=None)

    def signal_handler(signal, frame):
        print('Keyboard Interrupt. Exiting gracefully.')
        queue.close()
        p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    while p.poll() is None:
        line = p.stdout.readline()  # This blocks until it receives a newline.
        if not line:
            break
        parse_inotifywait_line(line, dirpath, queue)

if __name__ == '__main__':
    main()
