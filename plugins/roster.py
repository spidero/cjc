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
import re
import os

import libxml2

import pyxmpp
import pyxmpp.roster

from cjc.plugin import PluginBase, NamedService
from cjc.ui import ListBuffer,ListBufferError
from cjc import common
from cjc import ui
from cjc import cjc_globals

theme_attrs=(
    ("roster.available_online", curses.COLOR_YELLOW,curses.COLOR_BLACK,curses.A_BOLD, curses.A_BOLD),
    ("roster.available_dnd", curses.COLOR_RED,curses.COLOR_BLACK,curses.A_NORMAL, curses.A_BOLD),
    ("roster.available_away", curses.COLOR_YELLOW,curses.COLOR_BLACK,curses.A_NORMAL, curses.A_BOLD),
    ("roster.available_xa", curses.COLOR_BLUE,curses.COLOR_BLACK,curses.A_NORMAL, curses.A_BOLD),
    ("roster.available_chat", curses.COLOR_GREEN,curses.COLOR_BLACK,curses.A_BOLD, curses.A_BOLD),
    ("roster.unavailable", curses.COLOR_WHITE,curses.COLOR_BLACK,curses.A_NORMAL|curses.A_DIM, curses.A_DIM),
)

theme_formats=(
    ("roster.group", "%(group)s:"),
    ("roster.group_none", "unfiled:"),
    ("roster.group_me", "me:"),
    ("roster.group_unknown", "not in roster:"),
    ("roster.unavailable", "%[roster.unavailable] %(aflag)s%(sflag)s%(name)-20s [%(J:jid:show)s] %(J:jid:status)s"),
    ("roster.available", "%[roster.available_%(J:jid:show)s] %(aflag)s%(sflag)s%(name)-20s [%(J:jid:show)s] %(J:jid:status)s"),
    ("roster.list", "[%(T:now)s] Roster:\n%{roster_groups}\n"),
    ("roster.list_group", "[%(T:now)s]   %(group)s:\n%{roster_group_items}"),
    ("roster.list_unavailable", "[%(T:now)s]   %{@roster.unavailable}\n"),
    ("roster.list_available", "[%(T:now)s]   %{@roster.unavailable}\n"),
)

# virtual groups
VG_ME=1
VG_UNKNOWN=2

class Plugin(PluginBase, NamedService):
    service_name = "roster"
    def __init__(self,app,name):
        PluginBase.__init__(self,app,name)
        self.available_settings={
            "show": ("Which items show - list of 'available','unavailable','chat',"
                    "'online','away','xa' or 'all'",list,self.set_show),
            "buffer_preference": ("Preference of roster buffers when switching to the next active buffer. If 0 then the buffer is not even shown in active buffer list.",int),
            "cache_file": ("Location of roster cache files."
                " /unset it not to keep local copy of the roster.",
                                        (unicode, None))
            }
        self.settings={
                "show": ["all"],
                "buffer_preference": 2,
                "cache_file": "%($HOME)s/.cjc/cache/%(jid)s.roster",
                }
        app.add_info_handler("rostername",self.info_rostername)
        app.add_info_handler("rostergroups",self.info_rostergroups)
        app.add_event_handler("roster updated",self.ev_roster_updated)
        app.add_event_handler("presence changed",self.ev_presence_changed)
        app.add_event_handler("layout changed",self.ev_layout_changed)
        app.add_event_handler("stream closed", self.ev_stream_closed)
        app.add_event_handler("config loaded", self.ev_config_loaded)
        cjc_globals.theme_manager.set_default_attrs(theme_attrs)
        cjc_globals.theme_manager.set_default_formats(theme_formats)
        self.buffer=ListBuffer("Roster")
        self.buffer.preference=self.settings["buffer_preference"]
        self.extra_items=[]
        ui.activate_cmdtable("roster",self)

    def info_rostername(self,k,v):
        if not v:
            return None
        return "Roster name",v

    def info_rostergroups(self,k,v):
        if not v:
            return None
        return "Roster groups",string.join(v,",")

    def set_show(self,oldval,newval):
        self.write_all()
    
    def ev_config_loaded(self, event, arg):
        if not self.cjc.roster:
            self.load_cache()

    def ev_roster_updated(self,event,arg):
        if not arg:
            for item in self.cjc.roster.get_items():
                for group,it in self.extra_items:
                    if not isinstance(it,pyxmpp.JID) or group!=VG_UNKNOWN:
                        continue
                    jid=item.jid
                    if group==VG_UNKNOWN and it==jid or it.bare()==jid:
                        self.extra_items.remove((VG_UNKNOWN,it))
                        if self.buffer.has_key((VG_UNKNOWN,it)):
                            self.buffer.remove_item((VG_UNKNOWN,it))
            self.write_all()
            return
        self.update_item(arg)

    def ev_presence_changed(self,event,arg):
        if arg:
            self.update_item(arg)

    def ev_layout_changed(self,event,arg):
        if self.cjc.roster_window:
            self.cjc.roster_window.set_buffer(self.buffer)

    def ev_stream_closed(self, event, arg):
        self.save_cache()

    @property
    def cache_filename(self):
        filename = self.settings.get("cache_file")
        if not filename:
            return
        if self.cjc.jid:
            my_jid = self.cjc.jid
        else:
            my_jid = self.cjc.settings.get("jid")
        if not my_jid:
            return
        filename = cjc_globals.theme_manager.substitute(filename,
                            {"jid": unicode(my_jid.bare())}).encode("utf-8")
        return os.path.expanduser(filename)

    def save_cache(self):
        if not self.cjc.roster:
            return
        filename = self.cache_filename
        if not filename:
            return
        parent = os.path.dirname(filename)
        if not os.path.exists(parent):
            try:
                os.makedirs(parent.encode("utf-8"))
            except (OSError, IOError), err:
                self.error("Cannot write roster cache file."
                                    "Cannot make directory {0}: {1}"
                                                    .format(parent, err))
                return
        try:
            with open(filename, "w") as roster_file:
                print >>roster_file, str(self.cjc.roster)
        except IOError, err:
            self.error("Cannot write roster cache file {0}: {1}"
                                                .format(filename, err))

    def load_cache(self):
        if self.cjc.roster:
            return
        filename = self.cache_filename
        if not filename or not os.path.exists(filename):
            return
        try:
            with open(filename, "r") as roster_file:
                roster_data = roster_file.read().strip()
        except IOError, err:
            self.warning("Cannot read roster cache file {0}: {1}"
                                                .format(filename, err))
            return
        if not roster_data:
            return
        try:
            xml = libxml2.parseDoc(roster_data)
            roster = pyxmpp.roster.Roster(xml.getRootElement())
        except (ValueError, libxml2.libxmlError), err:
            self.warning("Cannot read roster cache file {0}: {1}"
                                                .format(filename, err))
            return
        self.cjc.roster = roster
        self.write_all()

    def update_item(self,item):
        self.debug("Roster.update_item(%r): %r" % (item, item))
        if isinstance(item,pyxmpp.JID):
            if self.cjc.roster:
                try:
                    item=self.cjc.roster.get_item_by_jid(item)
                except KeyError:
                    try:
                        item=self.cjc.roster.get_item_by_jid(item.bare())
                    except KeyError:
                        pass
        if isinstance(item,pyxmpp.roster.RosterItem):
            jid=item.jid
            groups=item.groups
            for g,j in self.buffer.get_keys():
                if j==jid and g not in groups:
                    self.buffer.remove_item((g,j))
                elif g==VG_UNKNOWN and j and (j==jid or j.bare()==jid):
                    self.buffer.remove_item((g,j))
            if groups:
                for group in groups:
                    self.write_item(group,item)
            else:
                self.write_item(None,item)
        elif isinstance(item,pyxmpp.JID):
            if item.bare()==self.cjc.jid.bare():
                group=VG_ME
                if not self.buffer.has_key((group,None)):
                    self.buffer.insert_themed((group,None),"roster.group_me",{})
            else:
                group=VG_UNKNOWN
                if not self.buffer.has_key((group,None)):
                    self.buffer.insert_themed((group,None),"roster.group_unknown",{})
            self.write_item(group,item)
            self.extra_items.append((group,item))
        self.buffer.update()

    def get_item_format_params(self,group,item,show_list):
        if isinstance(item,pyxmpp.JID):
            jid=item
            name=None
            ask=None
            if group==VG_ME:
                subs="both"
            else:
                subs="none"
        else:
            subs=item.subscription
            jid=item.jid
            name=item.name
            ask=item.ask
            if jid.resource:
                self.cjc.set_user_info(jid,"rostername",name)
            else:
                self.cjc.set_bare_user_info(jid,"rostername",name)

        if not name:
            name=jid.as_unicode()
        p={"name":name,"jid":jid}
        pr=self.cjc.get_user_info(jid,"presence")
        if not pr or pr.get_type() and pr.get_type()!="available":
            available=0
            show=None
        else:
            available=1
            show=pr.get_show()
            if not show:
                show="online"

        if subs=="remove":
            show_it=0
        elif "all" in show_list:
            show_it=1
        elif "available" in show_list and available:
            show_it=1
        elif "unavailable" in show_list and not available:
            show_it=1
        elif show in show_list and available:
            show_it=1
        else:
            show_it=0

        if not show_it:
            return None

        p["ask"]=ask
        if not ask:
            p["aflag"]=" "
        elif ask=="unsubscribe":
            p["aflag"]="-"
        else:
            p["aflag"]="?"
        p["subscription"]=subs
        if subs=="both":
            p["sflag"]=" "
        elif subs=="from":
            p["sflag"]="<"
        elif subs=="to":
            p["sflag"]=">"
        else:
            p["sflag"]="-"
        p["available"]=available
        return p

    def write_item(self,group,item):
        self.debug("Roster.write_item(%r): %r" % (item, item))
        if not self.buffer.has_key((group,None)):
            if group:
                p={"group":group}
                self.buffer.insert_themed((group,None),"roster.group",p)
            else:
                self.buffer.insert_themed((group,None),"roster.group_none",{})

        params=self.get_item_format_params(group,item,self.settings["show"])

        if params is None:
            self.debug("Roster.write_item: removing item")
            if isinstance(item,pyxmpp.JID):
                jid=item
            else:
                jid=item.jid
            try:
                self.buffer.remove_item((group,jid))
            except ListBufferError:
                pass
            return

        self.debug("Roster.write_item: updating item")
        if params["available"]:
            self.buffer.insert_themed((group,params["jid"]),"roster.available",params)
        else:
            self.buffer.insert_themed((group,params["jid"]),"roster.unavailable",params)

    def write_all(self):
        self.buffer.clear()
        groups_added=[]
        for group,item in self.extra_items:
            if group==VG_ME and group not in groups_added:
                self.buffer.insert_themed((group,None),"roster.group_me",{})
            elif group==VG_UNKNOWN and group not in groups_added:
                self.buffer.insert_themed((group,None),"roster.group_unknown",{})
            self.write_item(group,item)

        if not self.cjc.roster:
            self.buffer.update()
            return
        groups=self.cjc.roster.get_groups()
        groups.sort()
        for group in groups:
            for item in self.cjc.roster.get_items_by_group(group):
                self.write_item(group,item)
        self.buffer.update()

    def session_started(self,stream):
        self.cjc.request_roster()
    
    def cmd_add(self,args):
        groups=[]
        while 1:
            arg=args.shift()
            if arg=="-group":
                groups.append(args.shift())
            else:
                break

        try:
            user=pyxmpp.JID(arg)
        except pyxmpp.JIDError:
            self.error("Bad JID!")
            return

        if user.bare()==self.cjc.stream.me.bare():
            self.error("Self presence subscription is automatic."
                    " Cannot add own JID to the roster.")
            return

        try:
            item=self.cjc.roster.get_item_by_jid(user)
            if item:
                self.error(u"User '%s' already in roster." % (unicode(user),))
                return
        except KeyError:
            pass

        name=args.all()
        if not name:
            name=None

        item=self.cjc.roster.add_item(user,name=name)
        item.groups=groups
        iq=item.make_roster_push()
        self.cjc.stream.send(iq)

    def cmd_remove(self,args):
        user=args.shift()
        args.finish()
        user=self.cjc.get_user(user)
        if user is None:
            return
        if user.bare()==self.cjc.stream.me.bare():
            self.error("Self presence subscription is automatic."
                    " Cannot remove own JID from the roster.")
            return
        try:
            item=self.cjc.roster.remove_item(user)
        except KeyError:
            self.error(u"There is no %s in roster" % (user.as_unicode()))
            return
        iq=item.make_roster_push()
        self.cjc.stream.send(iq)

    def cmd_rename(self,args):
        user=args.shift()
        users=self.cjc.get_users(user)
        if not users:
            return
        for user in users:
            if user.bare()==self.cjc.stream.me.bare():
                self.error("Self presence subscription is automatic."
                        " Cannot rename own JID in the roster.")
                continue
            name=args.all()
            try:
                item=self.cjc.roster.get_item_by_jid(user)
            except KeyError:
                self.error(u"You don't have %s in your roster" % (user.as_unicode(),))
                continue
            item.name=name
            iq=item.make_roster_push()
            self.cjc.stream.send(iq)

    def cmd_group(self,args):
        user=args.shift()
        if user is None:
            self.error(u"/group without arguments!")
            return
        users=self.cjc.get_users(user)
        if not users:
            return

        groups=[]
        groups_add=[]
        groups_remove=[]
        while 1:
            group=args.shift()
            if not group:
                break
            if group.startswith("+"):
                groups_add.append(group[1:])
            elif group.startswith("-"):
                groups_remove.append(group[1:])
            else:
                groups.append(group)
        if groups and (groups_add or groups_remove):
            self.error("You cannot use groups with +/- sign and"
                    " without it in one /group command")
            return

        for user in users:
            if user.bare()==self.cjc.stream.me.bare():
                self.error("Self presence subscription is automatic."
                        " Cannot group own JID in the roster.")
                continue
            try:
                item = self.cjc.roster.get_item_by_jid(user)
            except KeyError:
                self.error(u"You don't have %s in your roster" 
                                                    % (user.as_unicode(),))
                continue

            if not groups and not groups_add and not groups_remove:
                self.info(u"User %s belongs to the following groups: %s"
                    % (user.as_unicode(), string.join(item.groups(),",")))
                continue

            if groups:
                item.groups=groups
            else:
                for group in groups_add:
                    if group not in item.groups:
                        item.groups.append(group)
                for group in groups_remove:
                    item.groups.remove(group)
            iq=item.make_roster_push()
            self.cjc.stream.send(iq)

    def filter_roster(self, args, eat_all_args = False, split_groups = False, extra_options = []):
        groups = []
        jids = []
        filter = []
        names = []
        subs = []
        while eat_all_args or (args.get() and args.get().startswith("-")):
            if args.get() in extra_options:
                break
            arg = args.shift()
            if arg is None:
                break
            if not arg.startswith("-"):
                try:
                    names.append(re.compile(arg))
                except re.error:
                    self.error(u"Invalid regular expression: %r" % (arg,))
                    return
                continue
            if arg=="-group":
                arg=args.shift()
                try:
                    groups.append(re.compile(arg))
                except re.error:
                    self.error(u"Invalid regular expression: %r" % (arg,))
                    return
            elif arg=="-jid":
                arg=args.shift()
                try:
                    jids.append(re.compile(arg))
                except re.error:
                    self.error(u"Invalid regular expression: %r" % (arg,))
                    return
            elif arg=="-state" or arg=="-show":
                arg=args.shift()
                if "," in arg:
                    filter+=arg.split(",")
                else:
                    filter.append(arg)
            elif arg == "-subscription" or arg == "-sub":
                arg = args.shift()
                if "," in arg:
                    subs += arg.split(",")
                else:
                    subs.append(arg)
            elif arg=="-name":
                arg=args.shift()
                try:
                    names.append(re.compile(arg))
                except re.error:
                    self.error(u"Invalid regular expression: %r" % (arg,))
                    return
            else:
                self.error("Bad roster filter option: %r" % (arg,))
                return

        if "all" in filter:
            filter = []

        def check_item(item):
            if names:
                if item.name is None:
                    return False
                ok=False
                for g in names:
                    if g.search(item.name):
                        ok=True
                        break
                if not ok:
                    return False
            if jids:
                if item.jid is None:
                    return False
                ok=False
                for g in jids:
                    if g.search(unicode(item.jid)):
                        ok=True
                        break
                if not ok:
                    return False
            if groups:
                if not item.groups:
                    return False
                ok = False
                for g in groups:
                    for ig in item.groups:
                        if g.search(ig):
                            ok=True
                            break
                if not ok:
                    return False
            if filter:
                ok = False
                for f in filter:
                    pr = self.cjc.get_user_info(item.jid, "presence")
                    if pr:
                        pr_type = pr.get_type()
                    if not pr or pr_type == "unavailable":
                        if f == "unavailable":
                            ok = True
                            break
                        continue
                    if pr_type == "error":
                        if f == "error":
                            ok = True
                            break
                        continue
                    if pr_type and pr_type != "available":
                        continue
                    if f == "online":
                        ok = True
                        break
                    if f == pr.get_show():
                        ok = True
                        break
                if not ok:
                    return False
            if subs:
                if not item.subscription in subs:
                    return False
            return True

        if split_groups:
            rgroups = self.cjc.roster.get_groups()
            rgroups.sort()
            result = []
            for group in rgroups:
                if groups:
                    if not group:
                        continue
                    ok=False
                    for g in groups:
                        if g.search(group):
                            ok=True
                            break
                    if not ok:
                        continue
                items = [(item.name,item.jid.as_unicode(),item) for item
                        in self.cjc.roster.get_items_by_group(group)
                        if check_item(item)]
                items.sort()
                result.append([group,[ item[2] for item in items ]])
            return result
        else:
            items = self.cjc.roster.get_items()
            return [ item for item in items if check_item(item) ]

    def cmd_list(self,args):
        if not self.cjc.roster:
            self.error("No roster available.")
            return
    
        groups = self.filter_roster(args, eat_all_args = True, split_groups = True)
        
        args.finish()

        formatted_list = []
        for group, items in groups:
            items = [(item.name,item.jid.as_unicode(), item) for item in items]
            items.sort()
            formatted_group = []
            for name, jid, item in items:
                params = self.get_item_format_params(group, item, "all")
                if not params:
                    continue
                if params["available"]:
                    formatted_group += cjc_globals.theme_manager.format_string(
                            "roster.list_available",params)
                else:
                    formatted_group += cjc_globals.theme_manager.format_string(
                            "roster.list_unavailable",params)
            if not formatted_group:
                continue
            if group is None:
                group=u"unfiled"
            params = {"roster_group_items": formatted_group, "group": group}
            formatted_list += cjc_globals.theme_manager.format_string("roster.list_group", params)
            
        if formatted_list:
            params = {"roster_groups": formatted_list}
            self.cjc.status_buf.append_themed("roster.list", params)
        else:
            self.error("No roster items matches your filter")
        self.cjc.status_buf.update()

roster_filter_cmd_args = ("-group opaque", "-jid opaque", "-state opaque", "-name opaque", "-subscription opaque")

ui.CommandTable("roster",50,(
    ui.Command("add",Plugin.cmd_add,
        "/add [-group group]... jid [name]",
        "Add a user to the roster (this doesn't automatically subscribe to his presence).",
        ("-group opaque","user","text")),
    ui.Command("remove",Plugin.cmd_remove,
        "/remove user",
        "Remove user from the roster.",
        ("user",)),
    ui.Command("rename",Plugin.cmd_rename,
        "/rename user name",
        "Change visible name of a user in the roster.",
        ("user","text")),
    ui.Command("group",Plugin.cmd_group,
        "/group user [+|-]group...",
        "Change groups a user from roster belongs to.",
        ("user","text")),
    ui.Command("list",Plugin.cmd_list,
        ("/list [-group regexp]..."
            " [-jid regexp]..."
            " [-state available|unavailable|online|offline|away|xa|chat|error]... "
            " [[-name] regexp]..."
            ),
        "List roster items",
        roster_filter_cmd_args + ("opaque", )),
    )).install()
# vi: sts=4 et sw=4
