import traceback
import sys
import uuid

import tornado.web
import tornado.ioloop
import numpy
import msgpack

from . import serializer


def get_server_handlers(local_catalog):
    return [
        (r"/v1/info", ServerInfoHandler, dict(local_catalog=local_catalog)),
        (r"/v1/source", ServerSourceHandler, dict(local_catalog=local_catalog)),
    ]


class ServerInfoHandler(tornado.web.RequestHandler):
    def initialize(self, local_catalog):
        self.local_catalog = local_catalog

    def get(self):
        sources = []
        for source in self.local_catalog.list():
            info = self.local_catalog.describe(source)
            info['name'] = source
            sources.append(info)

        server_info = dict(version='0.0.1', sources=sources)
        self.write(msgpack.packb(server_info, use_bin_type=True))

OPEN_SOURCES = {}


class ServerSourceHandler(tornado.web.RequestHandler):
    def initialize(self, local_catalog):
        self._local_catalog = local_catalog

    @tornado.web.asynchronous
    def post(self):
        request = msgpack.unpackb(self.request.body, encoding='utf-8')
        action = request['action']

        if action == 'open':
            entry_name = request['name']
            user_parameters = request['parameters']
            source = self._local_catalog.get(entry_name, **user_parameters)
            source.discover()
            source_id = str(uuid.uuid4())
            OPEN_SOURCES[source_id] = ClientState(source_id, source)

            response = dict(datashape=source.datashape, dtype=numpy.dtype(source.dtype).descr, shape=source.shape, container=source.container, source_id=source_id)
            self.write(msgpack.packb(response))
            self.finish()
        elif action == 'read':
            source_id = request['source_id']
            state = OPEN_SOURCES[source_id]
            accepted_formats = request['accepted_formats']

            self._chunk_encoder = self._pick_encoder(accepted_formats, state.source.container)
            self._iterator = state.source.read_chunks()
            self._container = state.source.container

            ioloop = tornado.ioloop.IOLoop.instance()
            ioloop.add_callback(self._write_callback)
        else:
            raise ArgumentException('%s not a valid source action' % action)

    def _pick_encoder(self, accepted_formats, container):
        for f in accepted_formats:
            if f in serializer.registry:
                encoder = serializer.registry[f]
                return encoder

        raise Exception('Unable to find compatible format')

    def _write_callback(self):
        try:
            chunk = next(self._iterator)
            data = self._chunk_encoder.encode(chunk, self._container)
            msg = dict(format=self._chunk_encoder.name, container=self._container, data=data)
            self.write(msgpack.packb(msg, use_bin_type=True))
            self.flush(callback=self._write_callback)
        except StopIteration:
            self.finish()


class ClientState:
    def __init__(self, source_id, source):
        self.source_id = source_id
        self.source = source