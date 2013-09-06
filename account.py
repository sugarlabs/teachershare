# Copyright (c) 2013 Walter Bender <walter@sugarlabs.org>
# Copyright (c) 2013 Martin Abente Lahaye <tch@sugarlabs.org>
# Copyright (c) 2013 Gonzalo Odiard <gonzalo@sugarlabs.org>
# Copyright (c) 2013 Agustin Zubiaga <aguz@sugarlabs.org>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA

from gettext import gettext as _

import logging
import base64
import os
import json
import telepathy
import dbus
import websocket
import tempfile
from zipfile import ZipFile
from threading import Thread

from gi.repository import Gtk
from gi.repository import GObject

from sugar3.datastore import datastore
from sugar3.graphics.alert import NotifyAlert
from sugar3.graphics.icon import Icon
from sugar3.graphics.menuitem import MenuItem
from sugar3.presence import presenceservice
from sugar3 import profile

from jarabe.journal import journalwindow
from jarabe.journal import model
from jarabe.webservice import account
from jarabe.model import neighborhood

ACCOUNT_NAME = _('Teacher')
ACCOUNT_ICON = 'female-7'
TARGET = 'org.sugarlabs.JournalShare'
JOURNAL_STREAM_SERVICE = 'journal-activity-http'
CHUNK_SIZE = 2048


class Account(account.Account):

    def __init__(self):
        self._shared_journal_entry = None
        self._model = neighborhood.get_model()
        self.unused_download_tubes = set()

    def get_description(self):
        return ACCOUNT_NAME

    def get_token_state(self):
        return self.STATE_VALID

    def get_shared_journal_entry(self):
        if self._shared_journal_entry is None:
            self._shared_journal_entry = _SharedJournalEntry(self)
        return self._shared_journal_entry


class _SharedJournalEntry(account.SharedJournalEntry):
    __gsignals__ = {
        'transfer-state-changed': (GObject.SignalFlags.RUN_FIRST, None,
                                   ([str])),
    }

    def __init__(self, webaccount):
        self._account = webaccount
        self._alert = None

    def get_share_menu(self, get_uid_list):
        menu = _ShareMenu(self._account, get_uid_list, True)
        self._connect_transfer_signals(menu)
        return menu

    def get_refresh_menu(self):
        menu = _RefreshMenu(self._account, False)
        self._connect_transfer_signals(menu)
        return menu

    def _connect_transfer_signals(self, transfer_widget):
        transfer_widget.connect('transfer-state-changed',
                                self.__display_alert_cb)

    def __display_alert_cb(self, widget, message):
        if self._alert is None:
            self._alert = NotifyAlert()
            self._alert.props.title = ACCOUNT_NAME
            self._alert.connect('response', self.__alert_response_cb)
            journalwindow.get_journal_window().add_alert(self._alert)
            self._alert.show()
        self._alert.props.msg = message

    def __alert_response_cb(self, alert, response_id):
        journalwindow.get_journal_window().remove_alert(alert)
        self._alert = None


class _RefreshMenu(MenuItem):
    __gsignals__ = {
        'transfer-state-changed': (GObject.SignalFlags.RUN_FIRST, None,
                                   ([str])),
        'comments-changed': (GObject.SignalFlags.RUN_FIRST, None, ([str]))
    }

    def __init__(self, webaccount, is_active):
        MenuItem.__init__(self, ACCOUNT_NAME)

        self._account = webaccount
        self._is_active = is_active

        self.set_image(Icon(icon_name=ACCOUNT_ICON,
                            icon_size=Gtk.IconSize.MENU))
        self.show()

        self.set_sensitive(False)

        # TODO: grab comments back from the teacher
        # self.connect('activate', self.__refresh_menu_cb)

    def set_metadata(self, metadata):
        self._metadata = metadata


class _ShareMenu(MenuItem):
    __gsignals__ = {
        'joined': (GObject.SignalFlags.RUN_FIRST, None, ([])),
        'transfer-state-changed': (GObject.SignalFlags.RUN_FIRST, None,
                                   ([str])),
        'comments-changed': (GObject.SignalFlags.RUN_FIRST, None, ([str]))
    }

    def __init__(self, webaccount, get_uid_list, is_active):
        MenuItem.__init__(self, ACCOUNT_NAME)

        self._account = webaccount
        self._activity_id = None
        self._shared_activity = None

        self.set_image(Icon(icon_name=ACCOUNT_ICON,
                            icon_size=Gtk.IconSize.MENU))
        self.show()

        self.set_sensitive(self._get_shared_activity_model())

        self._get_uid_list = get_uid_list

        # In this callback join the Journal Share activity
        self.connect('activate', self.__share_menu_cb)

    def _get_shared_activity_model(self):
        for activity_model in self._account._model.get_activities():
            logging.debug(activity_model.bundle.get_bundle_id())
            if activity_model.bundle.get_bundle_id() == TARGET:
                self._activity_id = activity_model.activity_id
                logging.debug('Found %s in the neighborhood' %
                              (TARGET))
                return True
        return False

    def _get_metadata(self):
        return model.get(self._get_uid_list()[0])

    def __share_menu_cb(self, menu_item):
        pservice = presenceservice.get_instance()
        if self._activity_id is not None:
            mesh_instance = pservice.get_activity(self._activity_id,
                                                  warn_if_none=False)
        else:
            logging.error('Cannot get activity from pservice.')
            self.emit('transfer-state-changed',
                      _('Cannot join Journal Share activity'))
            return

        self._set_up_sharing(mesh_instance)

    # We set up sharing in the same way as
    # sugar-toolkit-gtk3/src/sugar3/activity/activity.py

    def _set_up_sharing(self, mesh_instance):
        logging.debug('*** Act %s, mesh instance %r',
                      self._activity_id, mesh_instance)
        # There's already an instance on the mesh, join it
        logging.debug('*** Act %s joining existing mesh instance %r',
                      self._activity_id, mesh_instance)
        self._shared_activity = mesh_instance

        self._join_id = self._shared_activity.connect('joined',
                                                      self.__joined_cb)
        self._shared_activity.join()

    def __joined_cb(self, activity, success, err):
        """Callback when join has finished"""
        self._shared_activity.disconnect(self._join_id)
        self._join_id = None
        if not success:
            logging.error('Failed to join activity: %s', err)
            self.emit('transfer-state-changed',
                      _('Cannot join Journal Share activity'))
            return

        # Once we have joined the activity, we mimic
        # JournalShare activity.py
        self._watch_for_tubes()
        GObject.idle_add(self._get_view_information)

    def _watch_for_tubes(self):
        """Watch for new tubes."""
        tubes_chan = self._shared_activity.telepathy_tubes_chan
        logging.debug(tubes_chan)
        tubes_chan[telepathy.CHANNEL_TYPE_TUBES].connect_to_signal(
            'NewTube', self.__new_tube_cb)
        tubes_chan[telepathy.CHANNEL_TYPE_TUBES].ListTubes(
            reply_handler=self.__list_tubes_reply_cb,
            error_handler=self.__list_tubes_error_cb)

    def __new_tube_cb(self, tube_id, initiator, tube_type, service, params,
                      state):
        """Callback when a new tube becomes available."""
        logging.debug('New tube: ID=%d initator=%d type=%d service=%s '
                      'params=%r state=%d', tube_id, initiator, tube_type,
                      service, params, state)

        if service == JOURNAL_STREAM_SERVICE:
            self._account.unused_download_tubes.add(tube_id)
            GObject.idle_add(self._get_view_information)

    def __list_tubes_reply_cb(self, tubes):
        """Callback when new tubes are available."""
        for tube_info in tubes:
            self.__new_tube_cb(*tube_info)

    def __list_tubes_error_cb(self, e):
        """Handle ListTubes error by logging."""
        logging.error('ListTubes() failed: %s', e)
        self.emit('transfer-state-changed', _('Cannot upload now'))

    def _get_view_information(self):
        # Pick an arbitrary tube we can try to connect to the server
        try:
            tube_id = self._account.unused_download_tubes.pop()
        except (ValueError, KeyError), e:
            logging.error('No tubes to connect from right now: %s',
                          e)
            self.emit('transfer-state-changed', _('Cannot upload now'))
            return False

        GObject.idle_add(self._set_view_url, tube_id)
        return False

    def _set_view_url(self, tube_id):
        chan = self._shared_activity.telepathy_tubes_chan
        iface = chan[telepathy.CHANNEL_TYPE_TUBES]
        addr = iface.AcceptStreamTube(
            tube_id,
            telepathy.SOCKET_ADDRESS_TYPE_IPV4,
            telepathy.SOCKET_ACCESS_CONTROL_LOCALHOST, 0,
            utf8_strings=True)
        logging.debug('Accepted stream tube: listening address is %r', addr)
        # SOCKET_ADDRESS_TYPE_IPV4 is defined to have addresses of type '(sq)'
        assert isinstance(addr, dbus.Struct)
        assert len(addr) == 2
        assert isinstance(addr[0], str)
        assert isinstance(addr[1], (int, long))
        assert addr[1] > 0 and addr[1] < 65536
        ip = addr[0]
        port = int(addr[1])

        logging.debug('http://%s:%d/web/index.html' % (ip, port))

        metadata = self._get_metadata()

        self._jobject = datastore.get(metadata['uid'])
        # Add the information about the user uploading this object
        user_data = get_user_data()
        self._jobject.metadata['shared_by'] = json.dumps(user_data)
        # And add a comment to the Journal entry
        if 'comments' in self._jobject.metadata:
            comments = json.loads(self._jobject.metadata['comments'])
        else:
            comments = []
        comments.append({'from':user_data['from'],
                         'message':_('I shared this.'),
                         'icon-color':'[%s,%s]' % (
                             user_data['icon'][0], user_data['icon'][1])})
        self._jobject.metadata['comments'] = json.dumps(comments)

        if self._jobject and self._jobject.file_path:
            tmp_path = '/tmp'
            packaged_file_path = package_ds_object(self._jobject, tmp_path)
            url = 'ws://%s:%d/websocket/upload' % (ip, port)
            uploader = Uploader(packaged_file_path, url)
            uploader.connect('uploaded', self.__uploaded_cb)
            GObject.idle_add(self.emit, 'transfer-state-changed',
                             _('Upload started'))
            uploader.start()

        return False

    def __uploaded_cb(self, uploader, xfer_successful):
        if xfer_successful:
            datastore.write(self._jobject,
                            update_mtime=False,
                            reply_handler=self.__datastore_write_cb,
                            error_handler=self.__datastore_write_error_cb)
            GObject.idle_add(self.emit, 'transfer-state-changed',
                             _('Upload completed'))
            self.emit('comments-changed', self._jobject.metadata['comments'])
        else:
            GObject.idle_add(self.emit, 'transfer-state-changed',
                             _('Upload failed'))

    def __datastore_write_cb(self):
        logging.debug('saved changes to local datastore')

    def __datastore_write_error_cb(self, error):
        logging.error('datastore_write_error_cb: %r' % error)


# From JournalShare/utils.py


class Uploader(GObject.GObject):

    __gsignals__ = {
        'uploaded': (GObject.SignalFlags.RUN_FIRST, None, ([bool])),
        'transfer-state-changed': (GObject.SignalFlags.RUN_FIRST, None,
                                   ([str]))
    }

    def __init__(self, file_path, url):
        GObject.GObject.__init__(self)
        logging.debug('websocket url %s', url)
        # base64 encode the file
        self._file = tempfile.TemporaryFile(mode='r+')
        base64.encode(open(file_path, 'r'), self._file)
        self._file.seek(0)

        self._ws = websocket.WebSocketApp(url,
                                          on_open=self._on_open,
                                          on_message=self._on_message,
                                          on_error=self._on_error,
                                          on_close=self._on_close)
        self._chunk = str(self._file.read(CHUNK_SIZE))

    def start(self):
        upload_loop = Thread(target=self._ws.run_forever)
        upload_loop.setDaemon(True)
        upload_loop.start()

    def _on_open(self, ws):
        if self._chunk != '':
            self._ws.send(self._chunk)
        else:
            self._ws.close()

    def _on_message(self, ws, message):
        self._chunk = self._file.read(CHUNK_SIZE)
        if self._chunk != '':
            self._ws.send(self._chunk)
        else:
            self._ws.close()

    def _on_error(self, ws, error):
        self.emit('transfer-state-changed', _('Upload failed'), False)

    def _on_close(self, ws):
        self._file.close()
        GObject.idle_add(self.emit, 'uploaded', True)


def get_user_data():
    """
    Create this structure:
    {"from": "Walter Bender", "icon": ["#FFC169", "#FF2B34"]}
    used to identify the owner of a shared object
    is compatible with how the comments are saved in
    http://wiki.sugarlabs.org/go/Features/Comment_box_in_journal_detail_view
    """
    xo_color = profile.get_color()
    data = {}
    data['from'] = profile.get_nick_name()
    data['icon'] = [xo_color.get_stroke_color(), xo_color.get_fill_color()]
    if isinstance(data['icon'][0], unicode):
        data['icon'][0] = data['icon'][0].encode('ascii', 'replace')
    if isinstance(data['icon'][1], unicode):
        data['icon'][1] = data['icon'][1].encode('ascii', 'replace')
    return data


def package_ds_object(dsobj, destination_path):
    """
    Creates a zipped file with the file associated to a journal object,
    the preview and the metadata
    """
    object_id = dsobj.object_id
    logging.debug('id %s', object_id)
    preview_path = None

    logging.debug('before preview')
    if 'preview' in dsobj.metadata:
        # TODO: copied from expandedentry.py
        # is needed because record is saving the preview encoded
        if dsobj.metadata['preview'][1:4] == 'PNG':
            preview = dsobj.metadata['preview']
        else:
            # TODO: We are close to be able to drop this.
            preview = base64.b64decode(dsobj.metadata['preview'])

        preview_path = os.path.join(destination_path,
                                    'preview_id_' + object_id)
        preview_file = open(preview_path, 'w')
        preview_file.write(preview)
        preview_file.close()

    logging.debug('before metadata')
    # create file with the metadata
    metadata_path = os.path.join(destination_path,
                                 'metadata_id_' + object_id)
    metadata_file = open(metadata_path, 'w')
    metadata = {}
    for key in dsobj.metadata.keys():
        if key not in ('object_id', 'preview', 'progress'):
            metadata[key] = dsobj.metadata[key]
    metadata['original_object_id'] = dsobj.object_id

    metadata_file.write(json.dumps(metadata))
    metadata_file.close()

    logging.debug('before create zip')

    # create a zip fileincluding metadata and preview
    # to be read from the web server
    file_path = os.path.join(destination_path, 'id_' + object_id + '.journal')

    with ZipFile(file_path, 'w') as myzip:
        if preview_path is not None:
            myzip.write(preview_path, 'preview')
        myzip.write(metadata_path, 'metadata')
        myzip.write(dsobj.file_path, 'data')
    return file_path


def get_account():
    return Account()
