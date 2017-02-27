#!/usr/bin/python2.4
# $Id$
# ==============================================================================
# Copyright 2008 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
#
# Asynchronous and threaded socket servers for the sendmail milter protocol.
#
# Example usage:
#"""
#   import asyncore
#   import ppymilterserver
#   import ppymilterbase
#
#   class MyHandler(ppymilterbase.PpyMilter):
#     def OnMailFrom(...):
#       ...
#   ...
#
#   # to run async server
#   ppymilterserver.AsyncPpyMilterServer(port, MyHandler)
#   asyncore.loop()
#
#   # to run threaded server
#   ppymilterserver.ThreadedPpyMilterServer(port, MyHandler)
#   ppymilterserver.loop()
#"""
#

__author__ = 'Eric DeFriez'

import asynchat
import asyncore
import binascii
import logging
import os
import socket
import struct
import sys
import time

if sys.version_info[0] == 2:
    import SocketServer
else:
    import socketserver as SocketServer

from . import ppymilterbase

logger = logging.getLogger('ppymilter')

MILTER_LEN_BYTES = 4  # from sendmail's include/libmilter/mfdef.h


class AsyncPpyMilterServer(asyncore.dispatcher):
  """Asynchronous server that handles connections from
  sendmail over a network socket using the milter protocol.
  """

  # TODO: allow network socket interface to be overridden
  def __init__(self, sock_info_or_port, milter_class, max_queued_connections=1024, map=None, context=None):
    """Constructs an AsyncPpyMilterServer.

    Args:
      sock_info_or_port: A (sock_family, sock_addr) tuple, or a numeric port
                         to listen on (TCP).
      milter_class: A class (not an instance) that handles callbacks for
                    milter commands (e.g. a child of the PpyMilter class).
      max_queued_connections: Maximum number of connections to allow to
                              queue up on socket awaiting accept().
    """
    self.map     = map
    self.context = context
    asyncore.dispatcher.__init__(self, map=self.map)
    self.__milter_class = milter_class
    sock_family = socket.AF_INET
    sock_type   = socket.SOCK_STREAM
    if isinstance(sock_info_or_port, tuple):
        # Assume sock_family, sock_addr:
        sock_family, sock_addr = sock_info_or_port
    else:
        # Assume TCP port:
        sock_addr = ('', sock_info_or_port)
    self.create_socket(sock_family, sock_type)
    self.set_reuse_addr()
    self.bind(sock_addr)
    self.listen(max_queued_connections)

  def handle_accept(self):
    """Callback function from asyncore to handle a connection dispatching."""
    try:
      connaddr = self.accept()
      if connaddr is None:
        return
      (conn, addr) = connaddr
    except socket.error as e:
      logger.error('warning: server accept() threw an exception ("%s")',
                        str(e))
      return
    AsyncPpyMilterServer.ConnectionHandler(conn, addr, self.__milter_class, self.map, self.handle_error, self.context)

  def handle_error(self):
    return False

  class ConnectionHandler(asynchat.async_chat):
    """A connection handling class that manages communication on a
    specific connection's network socket.  Receives callbacks from asynchat
    when new data appears on a socket and when an entire milter command is
    ready invokes the milter dispatching class.
    """

    # TODO: allow milter dispatcher to be overridden (PpyMilterDispatcher)?
    def __init__(self, conn, addr, milter_class, map=None, on_error=None, context=None):
      """A connection handling class to manage communication on this socket.

      Args:
        conn: The socket connection object.
        addr: The address (port/ip) as returned by socket.accept()
        milter_class: A class (not an instance) that handles callbacks for
                      milter commands (e.g. a child of the PpyMilter class).
      """
      asynchat.async_chat.__init__(self, conn, map)
      self.__conn = conn
      self.__addr = addr
      self.__milter_dispatcher = ppymilterbase.PpyMilterDispatcher(milter_class, on_error, context)
      self.__input = []
      self.set_terminator(MILTER_LEN_BYTES)
      self.found_terminator = self.read_packetlen

    def collect_incoming_data(self, data):
      """Callback from asynchat--simply buffer partial data in a string."""
      self.__input.append(data)

    def log_info(self, message, type='info'):
      """Provide useful logging for uncaught exceptions"""
      if type == 'info':
        logger.debug(message)
      else:
        logger.error(message)

    def read_packetlen(self):
      """Callback from asynchat once we have an integer accumulated in our
      input buffer (the milter packet length)."""
      packetlen = int(struct.unpack('!I', b"".join(self.__input))[0])
      self.__input = []
      self.set_terminator(packetlen)
      self.found_terminator = self.read_milter_data

    def __send_response(self, response):
      """Send data down the milter socket.

      Args:
        response: The data to send.
      """
      if isinstance(response, str):
          response = response.encode()
      logger.debug('  >>> %s', binascii.b2a_qp(chr(response[0]).encode()))
      self.push(struct.pack('!I', len(response)))
      self.push(response)

    def read_milter_data(self):
      """Callback from asynchat once we have read the milter packet length
      worth of bytes on the socket and it is accumulated in our input buffer
      (which is the milter command + data to send to the dispatcher)."""
      inbuff = b"".join(self.__input)
      self.__input = []
      logger.debug('  <<< %s', binascii.b2a_qp(inbuff))
      try:
        response = self.__milter_dispatcher.Dispatch(inbuff)
        if type(response) == list:
          for r in response:
            self.__send_response(r)
        elif response:
          self.__send_response(response)

        # rinse and repeat :)
        self.found_terminator = self.read_packetlen
        self.set_terminator(MILTER_LEN_BYTES)
      except ppymilterbase.PpyMilterCloseConnection as e:
        logger.info('Closing connection ("%s")', str(e))
        self.close()


class ThreadedPpyMilterServer(SocketServer.ThreadingTCPServer):

  allow_reuse_address = True

  def __init__(self, port, milter_class, context=None):
    SocketServer.ThreadingTCPServer.__init__(self, ('', port),
                                    ThreadedPpyMilterServer.ConnectionHandler)
    self.milter_class = milter_class
    self.context = context
    self.loop = self.serve_forever

  def handle_error(self):
    return False


  class ConnectionHandler(SocketServer.BaseRequestHandler):
    def setup(self):
      self.request.setblocking(True)
      self.__milter_dispatcher = ppymilterbase.PpyMilterDispatcher(
          self.server.milter_class, self.server.handle_error, self.server.context)

    def __send_response(self, response):
      """Send data down the milter socket.

      Args:
        response: the data to send
      """
      if isinstance(response, str):
          response = response.encode()
      logger.debug('  >>> %s', binascii.b2a_qp(chr(response[0]).encode()))
      self.request.send(struct.pack('!I', len(response)))
      self.request.send(response)

    def handle(self):
      try:
        while True:
          packetlen = int(struct.unpack('!I',
                                        self.request.recv(MILTER_LEN_BYTES))[0])
          inbuf = []
          read = 0
          while read < packetlen:
            partial_data = self.request.recv(packetlen - read)
            inbuf.append(partial_data)
            read += len(partial_data)
          data = b"".join(inbuf)
          logger.debug('  <<< %s', binascii.b2a_qp(data))
          try:
            response = self.__milter_dispatcher.Dispatch(data)
            if type(response) == list:
              for r in response:
                self.__send_response(r)
            elif response:
              self.__send_response(response)
          except ppymilterbase.PpyMilterCloseConnection as e:
            logger.info('Closing connection ("%s")', str(e))
            break
      except Exception:
        # use similar error production as asyncore as they already make
        # good 1 line errors - similar to handle_error in asyncore.py
        # proper cleanup happens regardless even if we catch this exception
        (nil, t, v, tbinfo) = asyncore.compact_traceback()
        logger.error('uncaptured python exception, closing channel %s '
                      '(%s:%s %s)' % (repr(self), t, v, tbinfo))

# Allow running the library directly to demonstrate a simple example invocation.
if __name__ == '__main__':
  port = 9999
  try: port = int(sys.argv[1])
  except Exception: pass

  logging.basicConfig(level=logging.DEBUG,
                      format='%(asctime)s %(levelname)s %(message)s',
                      datefmt='%Y-%m-%d@%H:%M:%S')

  server = AsyncPpyMilterServer(port, ppymilterbase.PpyMilter)
  asyncore.loop()

  #server = ThreadedPpyMilterServer(port, ppymilterbase.PpyMilter)
  #server.loop()
