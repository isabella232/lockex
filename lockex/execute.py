'''
Lock and execution script
'''

from __future__ import (absolute_import, division, print_function, unicode_literals)

import atexit
import os
import signal
import socket
import subprocess
import sys
import time

import click
import psutil

from kazoo.client import KazooClient, KazooState
from kazoo.exceptions import LockTimeout
from kazoo.handlers.threading import KazooTimeoutError
import lockex.glog as log

click.disable_unicode_literals_warning = True


@click.command()
@click.option('--blocking/--no-blocking', help='Block and wait if lock is acquired by another process', default=True)
@click.option('--concurrent', '-c', help='Number of concurrent locks (leases) available, if this is set all clients must have the same value', default=1)
@click.option('--lockname', '-l', help='Name of lock, if no name is given, a lock name is automatically generated', default='lockex')
@click.option('--locktimeout', '-t', help='Timeout for waiting for lock aquistion, the default is to wait forever', default=None, type=click.FLOAT)
@click.option('--timeout', '-T', help='Timeout for connecting to zk', default=30)
@click.option('--zkhosts', '-z', envvar='ZKHOSTS', help='List of comma seperated zookeeper hosts, in the form of hostname:port', default='localhost:2181')
@click.argument('command', nargs=-1, metavar='<command>')
def execute(blocking, command, concurrent, lockname, locktimeout, timeout, zkhosts,):
    '''
    Main execution logic of getting a lock and executing the user supplied command
    '''
    if command:
        command = " ".join(command).strip()
    else:
        sys.exit(1)

    command_hash = str(abs(hash(command)))
    resource = "{0}:{1}".format(socket.gethostname(), os.getpid())
    lockname = "/{0}/{1}".format(lockname, command_hash)

    conn = get_zk(zkhosts, timeout)
    log.info("Locking with zkhosts={zkhosts} lockname={lockname} resource={resource} concurrent={concurrent} blocking={blocking} command='{command}'"
             .format(zkhosts=zkhosts, lockname=lockname, resource=resource, concurrent=concurrent, blocking=blocking, command=command))
    lock = conn.Semaphore(lockname, resource, concurrent)
    try:
        log.info("lease_holders='{0}'".format(",".join(lock.lease_holders())))
        log.info("Want to execute command={command}".format(command=command))
        if lock.acquire(blocking=blocking, timeout=locktimeout):
            log.debug("Executing command={command}")
            job = subprocess.Popen(command, stdout=sys.stdout, stderr=sys.stderr, shell=True)
            add_signal_helper(job)
            while job.returncode is None:
                job.poll()
                sys.stdout.flush()
                sys.stderr.flush()
                if job.returncode is None:
                    time.sleep(3)
            kill_job(job)
            lock.release()
            conn.stop()
            if job:
                sys.exit(job.returncode)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
        try:
            atexit.register(cleanup, job=job, lock=lock, conn=conn)
        except UnboundLocalError:
            atexit.register(cleanup, lock=lock, conn=conn)
        sys.exit(1)
    except LockTimeout as exc:
        log.info(exc)
        atexit.register(cleanup, lock=lock, conn=conn)
        sys.exit(1)


def cleanup(conn, lock, job=None):
    ''' Generic cleanup method '''
    if job:
        kill_job(job)
        job.wait()
    lock.release()
    conn.stop()
    os.system('stty sane')


def kill_job(job):
    '''
    Kill a subprocess popen job and it's children
    '''
    if job:
        try:
            kill(job.pid)
        except psutil.NoSuchProcess as exc:
            log.error("{0}. May have exited already".format(exc))


def kill(pid):
    '''
    Kill a pid
    '''
    process = psutil.Process(pid)
    try:
        for proc in process.get_children(recursive=True) + [process]:
            try:
                log.info("Killing pid={0}".format(proc.pid))
                proc.kill()
                time.sleep(0.1)
                proc.terminate()
            except psutil.NoSuchProcess as exc:
                log.error(exc)
    except AttributeError:
        log.info("Killing pid={0}".format(process.pid))
        process.kill()
        time.sleep(0.1)
        proc.terminate()


def get_zk(zkhosts, timeout):
    '''
    Initiate a zookeeper connection and add a listener
    '''
    conn = KazooClient(hosts=zkhosts, timeout=timeout)
    conn.add_listener(listener)
    try:
        conn.start()
    except KazooTimeoutError as exc:
        log.error(exc)
        sys.exit(1)
    return conn


def listener(state):
    '''
    Default listner to log events
    '''
    if state == KazooState.LOST:
        log.error(state)
    elif state == KazooState.SUSPENDED:
        log.error(state)
    else:
        pass


def add_signal_helper(process):
    '''
    Signal helper, accepts a psutil process object
    '''
    def handle_sig(sig, frame):
        ''' Kill and exit '''
        kill(process.pid)
        process.wait()
        os.system('stty sane')
        sys.exit(process.returncode)

    def reap(sig, frame):
        ''' do nothing '''
        pass

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGHUP, reap)
    signal.signal(signal.SIGINT, reap)
    signal.signal(signal.SIGUSR1, reap)
    signal.signal(signal.SIGUSR2, reap)
    signal.signal(signal.SIGQUIT, reap)
    signal.signal(signal.SIGCHLD, reap)


if __name__ == '__main__':
    execute()  # pylint: disable=no-value-for-parameter