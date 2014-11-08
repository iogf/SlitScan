#!/usr/bin/python
"""
 *  COPYRIGHT 2014 (C) Jason Volk
 *  COPYRIGHT 2014 (C) Svetlana Tkachenko
 *
 *  DISTRIBUTED UNDER THE GNU GENERAL PUBLIC LICENSE (GPL) (see: LICENSE)
"""

import os
import re
import sys
import stat
import time
import select
import socket
from random import choice
from string import printable
from socket import AF_INET, SOCK_STREAM
from select import POLLIN, POLLOUT, POLLPRI, POLLERR, POLLHUP, POLLNVAL
from collections import OrderedDict, defaultdict


# Constants
MAX_SOCKETS     = 32                              # Effective limit for the maximum number of simultaneous proxy tests.
TIMEOUT         = 15                              # Proxy communication timeout
LISTEN_IP       = "0.0.0.0"                       # The bind IP where we listen for proxy callbacks
LISTEN_PORT     = 16667                           # The bind port where we listen for proxy callbacks
CALLBACK_IP     = LISTEN_IP                       # Actual IP that the proxy gets told to connect to
CALLBACK_PORT   = LISTEN_PORT                     # Actual port that the proxy gets told to connect to
HARVEST_DIR     = "harvest/"                      # Harvesters should be placed in this subdirectory.
HARVEST_FIFO    = HARVEST_DIR + "harvest.fifo"    # Harvesters write to the FIFO at this path.
LOGFILE         = "slitscan.log"                  # Results go here


# Exceptions
class BadState(Exception):     pass
class Disconnected(IOError):   pass
class Discord(Disconnected):   pass



###############################################################################
# General utils


def remstr(remote):
	return "%s:%d" % (remote[0],remote[1])

def remstrf(remote):
	return "%-15s %-5d" % (remote[0],remote[1])

def remserial(str):
	return tuple(str.split(":"))



###############################################################################
# Logging / output


def stdout(str):
	sys.stdout.write("[%f] %s\n" % (time.time(),str))

def stderr(str):
	sys.stderr.write("[%f] %s\n" % (time.time(),str))

logfile = open(LOGFILE,'a')
def stdlog(str):
	str = re.sub(r"\x1b[^m]*m","",str)
	logfile.write("[%f] %s\n" % (time.time(),str))
	logfile.flush()



###############################################################################
# Event state.
#
# We base all events off a poll() of file descriptors that have activity.
#  * One file descriptor is a FIFO in the harvester directory.
#  * One file descriptor is a bound listening socket that accepts connect-backs from the proxies.
#  * Some fd's are connect-back clients accepted by the listener.
#  * The rest of the fd's are client sockets which are actively testing some proxy.
#
# The FIFO accepts input in the form of ascii strings ending with a newline.
# ex: "1.2.3.4:8080\n4.3.2.1:3128\n"
# The queue is the staging area after reading from the FIFO and before testing.
#
# A Client class is defined around the file descriptors registered with poll().
# Each client socket, FIFO device, and bound listener socket is based off a file
# descriptor number, thus we specialize Fifo and Listener off the Client base.


states =\
{
	"INITIATED":         0,
	"ESTABLISHED":       1,
	"SENT_CONNECT":      2,
	"RECV_CODE":         3,
	"SAME_BACK":         4,
	"DIFF_BACK":         5,
	"SENT_TOKEN":        6,
	"RECV_TOKEN":        7,
	"DISCOVERED":        8,
}

def statekeys(val):
	return [k for k,v in states.iteritems() if v == val]

def statekey(val):
	return statekeys(val)[0]


class Client(object):
	def __init__(self,
	             sock      = None,
	             mask      = 0,
	             remote    = None,
	             state     = states["INITIATED"]):
		self.sock      = sock
		self.mask      = mask
		self.remote    = remote
		self.state     = state
		self.code      = 0
		self.token     = None

	def getfd(self):         return self.sock.fileno()
	def getip(self):         return self.remote[0]
	def getport(self):       return self.remote[1]
	def statekey(self):      return statekey(self.state)
	def remstr(self):        return remstr(self.remote) if self.remote is not None else "---"
	def remstrf(self):       return remstrf(self.remote) if self.remote is not None else "---"
	def fdstrf(self):        return "%4d" % self.getfd()
	def maskstrf(self):      return "%-03x" % self.mask
	def fdmaskstrf(self):    return "%s | %s" % (self.fdstrf(),self.maskstrf())
	def statestrf(self):     return "%3d %1d | %-16s" % (self.code,self.state,self.statekey())
	def __str__(self):       return "%s | %-16s | %-21s" % (self.fdmaskstrf(),self.statestrf(),self.remstrf())
	def _log(self,sym,msg):  return "%s %s : %s" % (sym,str(self),msg)
	def log(self,sym,msg):
		str = self._log(sym,msg)
		stdout(str)
		stdlog(str)


# FIFO interface - perusing as a special Client case
class Fifo(Client):
	def __init__(self, path):
		self.path = path
		self.dir = os.path.split(path)[0]

		if not os.path.exists(self.dir):
			os.mkdir(self.dir)

		if not self._exists():
			os.mkfifo(self.path)

		if not self._check():
			raise Exception("File @ %s is not a FIFO!" % self.path)

		super(Fifo,self).__init__(self._fopen(),
		                          POLLIN | POLLHUP | POLLERR | POLLNVAL)

	def next(self):
		for remote in self._nextrems():
			yield remote

	def reopen(self):       os.dup2(self._open(),self.getfd())
	def _exists(self):      return os.path.exists(self.path)
	def _check(self):       return stat.S_ISFIFO(os.stat(self.path).st_mode)
	def _open(self):        return os.open(self.path,os.O_RDONLY|os.O_NONBLOCK)
	def _fopen(self):       return os.fdopen(self._open(),'r',65536)
	def _valid(self,line):  return re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\:\d{1,5}$",line)
	def _nextlines(self):   return [line for line in self.sock.read().split("\n") if self._valid(line)]
	def _nextparts(self):   return map(lambda line: line.partition(":"), self._nextlines())
	def _nextrems(self):    return map(lambda part: (part[0],int(part[2])), self._nextparts())
	def __iter__(self):     return self.next()



# Listener interface - perusing as a special Client case
class Listener(Client):
	def __init__(self, ip, port):
		super(Listener,self).__init__(socket.socket(AF_INET,SOCK_STREAM),
		                              POLLIN | POLLHUP | POLLERR | POLLNVAL,
		                              (ip,port))
		self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
		self.sock.bind(self.remote)
		self.sock.listen(MAX_SOCKETS)



# Runtime state
poll      = select.poll()                         # The poll object, the only blocking part of the program.
queue     = OrderedDict()                         # Stage area. OrderedDict is used for deduplication.
listener  = Listener(LISTEN_IP,LISTEN_PORT)       # The bound listening socket for connect-backs.
fifo      = Fifo(HARVEST_FIFO)                    # The input FIFO object.
ips       = {}                                    # Map of IPs to fd numbers (not remotes, just IPs)
tokens    = {}                                    # Map of nonce codes to fd numbers
fds       = {}                                    # Map of active fd numbers to Client objects, including fifo/listener


def statestr():
	return "q: %d fds: %d ips: %d tok: %d" % (len(queue),len(fds),len(ips),len(tokens))

def registered(remote):
	return any([remote == client.remote for client in fds.itervalues()])

def netclient(client):
	return client.getfd() != fifo.getfd() and client.getfd() != listener.getfd()

def netclients():
	return [client for fd, client in fds.iteritems() if netclient(client)]

def sockclients():
	return [client for client in netclients() if client.sock is not None]

def remask(client):
	poll.modify(client.getfd(),client.mask)

def register(client):
	poll.register(client.getfd(),client.mask)
	fds[client.getfd()] = client

	if client.remote is not None:
		ips[client.getip()] = client.getfd()

	if client.token is not None:
		tokens[client.token] = client.getfd()


def unregister(client):
	poll.unregister(client.getfd())

	if client.getfd() in fds:
		del fds[client.getfd()]

	if client.remote is not None and client.getip() in ips:
		del ips[client.getip()]

	if client.token in tokens:
		del tokens[client.token]



###############################################################################
# Harvester directory & related
# Handling FIFO events
#


def harvesters():
	return [file for file in os.listdir(HARVEST_DIR) if not file.endswith("fifo")]


def enqueue_fifo(fifo):
	before = len(queue)
	for remote in fifo:
		queue[remote] = None

	got = len(queue) - before
	fifo.log("\033[1;47;30m**\033[0m","Received %d new remotes (%s)" % (got,statestr()))


def handle_fifo(fifo, ev):
	if ev & POLLIN:      enqueue_fifo(fifo)
	if ev & POLLHUP:     fifo.reopen()
	if ev & POLLERR:     raise Exception("FIFO POLLERR")
	if ev & POLLNVAL:    raise Exception("FIFO POLLNVAL")



###############################################################################
# Connect-back Listener


def handle_listener_diff_back(conn, remote):
	client = Client(conn,POLLIN|POLLHUP|POLLERR|POLLNVAL,remote,states["DIFF_BACK"])
	client.log("\033[1;46;37m<|\033[0m","\033[0;35Connection from unknown IP.\033[0m")
	register(client)


def handle_listener_same_back(conn, remote):
	client = fds[ips[remote[0]]]
	client.state = states["SAME_BACK"]
	client.log("\033[1;46;37m><\033[0m","\033[0;36mConnected back from source IP.\033[0m")
	client.sock.shutdown(socket.SHUT_RDWR)
	conn.shutdown(socket.SHUT_RDWR)
	conn.close()


def handle_listener_accept(conn, remote):
	conn.setblocking(0)
	if remote[0] not in ips:
		handle_listener_diff_back(conn,remote)
	else:
		handle_listener_same_back(conn,remote)


def handle_listener(listener,ev):
	if ev & POLLIN:        handle_listener_accept(*listener.sock.accept())
	if ev & POLLHUP:       raise Exception("LISTENER POLLHUP")
	if ev & POLLERR:       raise Exception("LISTENER POLLERR")
	if ev & POLLNVAL:      raise Exception("LISTENER POLLNVAL")



###############################################################################
# Client events handlers


#######
# SEND

def send_token(client):
 	client.token = "".join([choice(printable) for i in xrange(0,64)])
	client.sock.sendall(client.token)
	client.state = states["SENT_TOKEN"]
	tokens[client.token] = client.getfd()
	client.log("\033[1;43;30m>>\033[0m","\033[1;32m%s\033[0m" % `client.token`)


def send_connect(client):
	pkg = "CONNECT %s:%d HTTP/1.0\r\n\r\n" % (CALLBACK_IP,CALLBACK_PORT)
	client.sock.sendall(pkg)
	client.state = states["SENT_CONNECT"]
	client.log("\033[0;42;37m>>\033[0m","\033[0;33m%s\033[0m" % `pkg`)


#######
# RECV

def handle_client_unexpected(client, data):
	client.log("\033[0;42;37m<<\033[0m","\033[1;33m%s\033[0m" % `data`)


def handle_client_token(client, data):
	if len(data) != 64:
		raise Discord("Did not get a proper token back: (%d) %s" % (len(data),`data`))

	if data not in tokens:
		raise Discord("Got an unrecognized token: %s" % `data`)

	client.state = states["RECV_TOKEN"]
	client.log("\033[1;42;35m<<\033[0m","\033[1;32mGot a token: %s\033[0m" % `data`)
	
	source = fds[tokens[data]]
	source.state = states["DISCOVERED"]
	source.log("\033[1;45m()\033[0m","\033[1;35mDiscovered tunnel to %s\033[0m" % client.remstr())
	source.sock.shutdown(socket.SHUT_RDWR)

	client.state = states["DISCOVERED"]
	client.log("\033[1;45m)(\033[0m","\033[1;35mDiscovered tunnel from %s\033[0m" % source.remstr())
	client.sock.shutdown(socket.SHUT_RDWR)


def handle_client_http(client, data):
	header = data.split(" ",2)
	if len(header) < 3:
		raise Discord("Bad HTTP header data: %s" % `header`)

	prot, code, msg = tuple(header)
	if prot != "HTTP/1.0" and prot != "HTTP/1.1":
		raise Discord("Bad HTTP protocol: %s" % `header`)

	if not code.isdigit():
		raise Discord("Bad HTTP code: %s" % `header`)

	client.code = int(code)
	client.state = states["RECV_CODE"]
	client.log("\033[1;42;37m<<\033[0m","\033[1;33m%s\033[0m" % `header`)

	if client.code != 200:
		raise Discord("Did not get 200")

	send_token(client)
	client.mask = POLLHUP | POLLERR | POLLNVAL
	remask(client)


def handle_client_recv(client):
	data = client.sock.recv(128)
	line = data.split("\r\n")[0]
	if client.state == states["SENT_CONNECT"]:   handle_client_http(client,line)
	elif client.state == states["DIFF_BACK"]:    handle_client_token(client,line)
	else:                                        handle_client_unexpected(client,line)


def handle_client_established(client):
	client.state = states["ESTABLISHED"]
	client.log("\033[0;42;37m||\033[0m","\033[0;33mConnection established\033[0m")
	send_connect(client)
	client.mask = POLLIN | POLLHUP | POLLERR | POLLNVAL
	remask(client)


def handle_client_error(client):
	client.sock.recv(0)                      # might throw something useful first
	raise Disconnected("Unknown error")


def handle_client_hangup(client):
	client.sock.recv(0)                      # might throw something useful first
	raise Disconnected("Connection closed")


def handle_client(client, ev):
	if ev & POLLNVAL:            raise Disconnected("INVALID")
	if ev & POLLHUP:             handle_client_hangup(client)
	if ev & POLLERR:             handle_client_error(client)
	if ev & POLLIN:              handle_client_recv(client)
	if ev & POLLOUT:             handle_client_established(client)



###############################################################################
# Dispatch poll events to handlers


def handle(fd, ev):
	try:
		if fd == listener.getfd():   handle_listener(listener,ev)
		elif fd == fifo.getfd():     handle_fifo(fifo,ev)
		else:                        handle_client(fds[fd],ev)

	except IOError as e:
		fds[fd].log("\033[1;41;37m--\033[0m","\033[1;31m%s\033[0m" % str(e))
		unregister(fds[fd])

	except Exception as e:
		stderr("(state: %s) \033[1;41;37m%s\033[0m" % (statestr(),str(e)))
		raise


###############################################################################
# Initiating new proxies


def connect(remote):
	client = Client()
	client.remote = remote
	client.mask = POLLIN | POLLOUT | POLLERR | POLLHUP | POLLNVAL
	client.sock = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
	client.sock.setblocking(0)
	client.sock.connect_ex(client.remote)
	client.log("\033[1;43;30m|>\033[0m","Attempting connect...")
	register(client)


def start():
	while len(queue) > 0 and len(fds) < MAX_SOCKETS:
		try:
			remote, none = queue.popitem(False)
			if not registered(remote):
				 connect(remote)

		except IOError as e:
			stderr("start(): %s" % str(e))

		except Exception as e:
			stderr("start(): %s" % str(e))
			raise


	
###############################################################################
# Main program loop

stdout("FIFO @ %s" % fifo.path)
stdout("Listening on %s" % listener.remstr())
stdout("Logging to %s" % LOGFILE)

register(fifo)
register(listener)

print "SlitScan starting..."

while 1:
	start()
	for fd, ev in poll.poll():
		handle(fd,ev)