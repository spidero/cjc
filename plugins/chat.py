# Console Jabber Client
# Copyright (C) 2004-2010 Jacek Konieczny
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as published
# by the Free Software Foundation
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

import string
import curses
import os
import logging
from datetime import datetime
import uuid

import pyxmpp
from pyxmpp.jabber import delay
from cjc import ui
from cjc.plugin import PluginBase, Archiver, Archive
from cjc import common
from cjc import cjc_globals

logger = logging.getLogger("cjc.plugin.chat")

theme_attrs=(
    ("chat.me", curses.COLOR_YELLOW,curses.COLOR_BLACK,curses.A_BOLD, curses.A_UNDERLINE),
    ("chat.peer", curses.COLOR_WHITE,curses.COLOR_BLACK,curses.A_NORMAL, curses.A_NORMAL),
    ("chat.info", curses.COLOR_GREEN,curses.COLOR_BLACK,curses.A_NORMAL, curses.A_NORMAL),
)

theme_formats=(
        ("chat.started",u"[%(T:timestamp)s] %[chat.info]* Chat with %(J:peer:nick)s (%(J:peer:jid)s) started\n"),
    ("chat.me",u"[%(T:timestamp)s] %[chat.me]<%(J:me:nick)s>%[] %(msg)s\n"),
    ("chat.peer",u"[%(T:timestamp)s] %[chat.peer]<%(J:peer:nick)s>%[] %(msg)s\n"),
    ("chat.action",u"[%(T:timestamp)s] %[chat.info]* %(J:jid:nick)s %(msg)s\n"),
    ("chat.descr",u"Chat with %(J:peer:full)s [%(J:peer:show)s] %(J:peer:status)s"),
    ("chat.day_change",u"%{@day_change}"),
)

class ChatBuffer(ui.TextBuffer):
    def __init__(self, conversation):
        self.conversation = conversation
        try:
            self.archive = cjc_globals.application.plugins.get_service(Archive)
        except KeyError:
            self.archive = None
        ui.TextBuffer.__init__(self, conversation.fparams, "chat.descr",
                                                "chat buffer", conversation)
        self.last_record = None
    def fill_top_underflow(self, lines_needed):
        if not self.archive:
            return
        if self.last_record:
            older_than = self.last_record
        else:
            older_than = self.conversation.start_time
        record_iter = self.archive.get_records('chat',
                        self.conversation.peer.bare(),
                        older_than = older_than, limit = lines_needed,
                        order = Archive.REVERSE_CHRONOLOGICAL)
        records = list(record_iter)
        if not records:
            return
        records.reverse()
        self.last_record = records[0][0]
        logger.debug("Got {0} records:".format(len(records)))
        for record_id, record in records:
            logger.debug("Record {0!r}: {1!r}".format(record_id, record))
            fparams = dict(self.conversation.fparams)
            if record.direction == "in":
                fparams["jid"] = record.peer
                theme_fmt = "chat.peer"
            else:
                fparams["jid"] = record.peer
                theme_fmt = "chat.me"
            if record.timestamp:
                fparams["timestamp"] = record.timestamp
            if record.body.startswith(u"/me "):
                fparams["msg"] = record.body[4:]
                self.append_themed("chat.action", fparams)
                return
            fparams["msg"] = record.body
            self.append_themed(theme_fmt, fparams)

class Conversation:
    def __init__(self,plugin,me,peer,thread=None, start_time = None):
        self.start_time = start_time if start_time else datetime.now()
        self.plugin=plugin
        self.me=me
        self.peer=peer
        if thread:
            self.thread=thread
            self.thread_inuse=1
        else:
            self.thread = unicode(uuid.uuid4())
            self.thread_inuse = 0
        self.fparams={
            "peer":self.peer,
            "jid":self.me,
        }
        self.buffer = ChatBuffer(self)
        self.buffer.preference=plugin.settings["buffer_preference"]
        self.buffer.user_input=self.user_input
        self.buffer.append_themed("chat.started",self.fparams)
        self.buffer.update()

    def change_peer(self,peer):
        self.peer=peer
        self.fparams["peer"]=peer
        self.buffer.update_info(self.fparams)

    def add_msg(self,s,format,who,timestamp=None):
        fparams=dict(self.fparams)
        fparams["jid"]=who
        if timestamp:
            fparams["timestamp"]=timestamp
        if s.startswith(u"/me "):
            fparams["msg"]=s[4:]
            self.buffer.append_themed("chat.action",fparams)
            self.buffer.update()
            return
        fparams["msg"]=s
        self.buffer.append_themed(format,fparams)
        self.buffer.update()

    def add_sent(self,s):
        self.add_msg(s,"chat.me",self.me)

    def add_received(self,s,timestamp):
        self.add_msg(s,"chat.peer",self.peer,timestamp)

    def user_input(self,s):
        if not self.plugin.cjc.stream:
            self.buffer.append_themed("error","Not connected")
            self.buffer.update()
            return 0

        m=pyxmpp.Message(to_jid=self.peer,stanza_type="chat",body=s,thread=self.thread)
        self.plugin.cjc.stream.send(m)
        self.add_sent(s)

        archivers = self.plugin.cjc.plugins.get_services(Archiver)
        for archiver in archivers:
            archiver.log_event("chat", self.peer, 'out', None, None, s, self.thread)

        return 1

    def error(self,stanza):
        err=stanza.get_error()
        emsg=err.get_message()
        msg="Error"
        if emsg:
            msg+=": %s" % emsg
        etxt=err.get_text()
        if etxt:
            msg+=" ('%s')" % etxt
        self.buffer.append_themed("error",msg)
        self.buffer.update()

    def cmd_me(self,args):
        if not args:
            return 1
        args=args.all()
        if not args:
            return 1
        self.user_input(u"/me "+args)
        return 1

    def cmd_close(self,args):
        args.finish()
        key=self.peer.bare().as_unicode()
        if self.plugin.conversations.has_key(key):
            l=self.plugin.conversations[key]
            if self in l:
                l.remove(self)
        self.buffer.close()
        return 1

    def cmd_whois(self,args):
        self.buffer.deactivate_command_table()
        try:
            if not args.get():
                args=ui.CommandArgs(self.peer.as_unicode())
            ui.run_command("whois",args)
        finally:
            self.buffer.activate_command_table()

    def cmd_info(self,args):
        self.buffer.deactivate_command_table()
        try:
            if not args.get():
                args=ui.CommandArgs(self.peer.as_unicode())
            ui.run_command("info",args)
        finally:
            self.buffer.activate_command_table()


ui.CommandTable("chat buffer",50,(
    ui.Command("me",Conversation.cmd_me,
        "/me text",
        "Sends /me text",
        ("text",)),
    ui.Command("close",Conversation.cmd_close,
        "/close",
        "Closes current chat buffer"),
    ui.Command("whois",Conversation.cmd_whois,
        "/whois [options] [user]",
        None),
    ui.Command("info",Conversation.cmd_info,
        "/info [options] [user]",
        None),
    )).install()

class Plugin(PluginBase):
    def __init__(self,app,name):
        PluginBase.__init__(self,app,name)
        self.conversations={}
        cjc_globals.theme_manager.set_default_attrs(theme_attrs)
        cjc_globals.theme_manager.set_default_formats(theme_formats)
        self.available_settings={
            "buffer_preference": ("Preference of chat buffers when switching to the next active buffer. If 0 then the buffer is not even shown in active buffer list.",int),
            "auto_popup": ("When enabled each new chat buffer is automatically made active.",bool),
            "merge_threads": ("Use single chat buffer for a single peer.",bool),
            }
        self.settings={
                "buffer_preference": 100,
                "auto_popup": False,
                "merge_threads": False,
                }
        app.add_event_handler("presence changed",self.ev_presence_changed)
        app.add_event_handler("day changed",self.ev_day_changed)
        ui.activate_cmdtable("chat",self)

    def cmd_chat(self,args):
        peer=args.shift()
        if not peer:
            self.error("/chat without arguments")
            return

        if not self.cjc.stream and args.get():
            self.error("Connect first!")
            return

        peer=self.cjc.get_best_user(peer)
        if peer is None:
            return

        conversation=Conversation(self,self.cjc.jid,peer)
        key=peer.bare().as_unicode()
        if self.conversations.has_key(key):
            self.conversations[key].append(conversation)
        else:
            self.conversations[key]=[conversation]

        text=args.all()
        if text:
            conversation.user_input(text)

        cjc_globals.screen.display_buffer(conversation.buffer)

    def ev_presence_changed(self,event,arg):
        key=arg.bare().as_unicode()
        if not self.conversations.has_key(key):
            return
        for conv in self.conversations[key]:
            if conv.peer==arg or conv.peer==arg.bare():
                conv.buffer.update_info(conv.fparams)

    def ev_day_changed(self,event,arg):
        for convs in self.conversations.values():
            for conv in convs:
                conv.buffer.append_themed("chat.day_change",{},activity_level=0)
                conv.buffer.update()

    def session_started(self,stream):
        self.cjc.stream.set_message_handler("chat",self.message_chat)
        self.cjc.stream.set_message_handler("error",self.message_error,None,90)

    def find_conversation(self, peer, thread, allow_other_thread = False):
        key = peer.bare().as_unicode()
        convs = self.conversations.get(key)
        if not convs:
            return None
        conv = None
        for c in convs:
            if not thread and (not c.thread or not c.thread_inuse):
                conv = c
                break
            if thread and thread == c.thread:
                conv = c
                break
        if conv:
            if conv.thread and not thread:
                # peer doesn't copy thread-id, do not use it
                conv.thread = None
            elif thread:
                conv.thread_inuse = 1
        elif thread and allow_other_thread:
            return convs[0]
        return conv

    def message_error(self,stanza):
        fr=stanza.get_from()
        thread=stanza.get_thread()
        
        conv = self.find_conversation(fr, thread, True)

        if not conv:
            return 0

        conv.error(stanza)
        return 1

    def message_chat(self,stanza):
        fr=stanza.get_from()
        thread=stanza.get_thread()
        subject=stanza.get_subject()
        body=stanza.get_body()
        if body is None:
            body=u""
        if subject:
            body=u"%s: %s" % (subject,body)
        elif not body:
            return

        d=delay.get_delay(stanza)
        if d:
            timestamp=d.get_datetime_local()
        else:
            timestamp=None

        conv = self.find_conversation(fr, thread, self.settings.get("merge_threads"))

        if not conv:
            conv=Conversation(self,self.cjc.jid,fr,thread, timestamp)
            key=fr.bare().as_unicode()
            if self.conversations.has_key(key):
                self.conversations[key].append(conv)
            else:
                self.conversations[key]=[conv]
            if self.settings.get("auto_popup"):
                cjc_globals.screen.display_buffer(conv.buffer)
            else:
                conv.buffer.update()
        else:
            if fr!=conv.peer:
                conv.change_peer(fr)

        self.cjc.send_event("chat message received",body)
        conv.add_received(body,timestamp)
        
        archivers = self.cjc.plugins.get_services(Archiver)
        for archiver in archivers:
            archiver.log_event("chat", fr, 'in', timestamp, subject, body, thread)

        return 1


ui.CommandTable("chat",51,(
    ui.Command("chat",Plugin.cmd_chat,
        "/chat nick|jid [text]",
        "Start chat with given user",
        ("user","text")),
    )).install()
# vi: sts=4 et sw=4
