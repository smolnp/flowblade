"""
    Flowblade Movie Editor is a nonlinear video editor.
    Copyright 2012 Janne Liljeblad.

    This file is part of Flowblade Movie Editor <http://code.google.com/p/flowblade>.

    Flowblade Movie Editor is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Flowblade Movie Editor is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with Flowblade Movie Editor. If not, see <http://www.gnu.org/licenses/>.
"""
from gi.repository import Gtk
from gi.repository import Gdk

import cairo
import copy
import hashlib
import mlt
import os
from os import listdir
from os.path import isfile, join
import subprocess
import shutil
import re
import sys
import threading
import time

import appconsts
import dialogutils
import edit
from editorstate import current_sequence
from editorstate import PROJECT
import gui
import gmicheadless
import gmicplayer
import jobs
import mltprofiles
import respaths
import toolsencoding
import userfolders
import utils

FULL_RENDER = 0
CLIP_LENGTH_RENDER = 1

OVERLAY_COLOR = (0.17, 0.23, 0.63, 0.5)
GMIC_TYPE_ICON = None

_status_polling_thread = None

# ----------------------------------------------------- interface
def get_action_object(container_data):
    if container_data.container_type == appconsts.CONTAINER_CLIP_GMIC:
        return GMicContainerActions(container_data)

def shutdown_polling():
    if _status_polling_thread == None:
        return
    
    _status_polling_thread.shutdown()
                

# ------------------------------------------------------------ thumbnail creation helpers
def _get_type_icon(container_type):
    global GMIC_TYPE_ICON
    
    if GMIC_TYPE_ICON == None:
        GMIC_TYPE_ICON = cairo.ImageSurface.create_from_png(respaths.IMAGE_PATH + "container_clip_gmic.png")
        
    
    if container_type == appconsts.CONTAINER_CLIP_GMIC:
        return GMIC_TYPE_ICON

def _write_thumbnail_image(profile, file_path):
    """
    Writes thumbnail image from file producer
    """
    # Get data
    md_str = hashlib.md5(file_path.encode('utf-8')).hexdigest()
    thumbnail_path = userfolders.get_cache_dir() + appconsts.THUMBNAILS_DIR + "/" + md_str +  ".png"

    # Create consumer
    consumer = mlt.Consumer(profile, "avformat", 
                                 thumbnail_path)
    consumer.set("real_time", 0)
    consumer.set("vcodec", "png")

    # Create one frame producer
    producer = mlt.Producer(profile, str(file_path))
    if producer.is_valid() == False:
        raise ProducerNotValidError(file_path)

    info = utils.get_file_producer_info(producer)

    length = producer.get_length()
    frame = length // 2
    producer = producer.cut(frame, frame)

    # Connect and write image
    consumer.connect(producer)
    consumer.run()
    
    return (thumbnail_path, length, info)

def _create_image_surface(icon_path):
    icon = cairo.ImageSurface.create_from_png(icon_path)
    scaled_icon = cairo.ImageSurface(cairo.FORMAT_ARGB32, appconsts.THUMB_WIDTH, appconsts.THUMB_HEIGHT)
    cr = cairo.Context(scaled_icon)
    cr.save()
    cr.scale(float(appconsts.THUMB_WIDTH) / float(icon.get_width()), float(appconsts.THUMB_HEIGHT) / float(icon.get_height()))
    cr.set_source_surface(icon, 0, 0)
    cr.paint()
    cr.restore()

    return (cr, scaled_icon)


# ---------------------------------------------------- action objects
class AbstractContainerActionObject:
    
    def __init__(self, container_data):
        self.container_data = container_data

    def create_data_dirs_if_needed(self):
        session_folder = self.get_session_dir()
        clip_frames_folder = session_folder + appconsts.CC_CLIP_FRAMES_DIR
        rendered_frames_folder = session_folder + appconsts.CC_RENDERED_FRAMES_DIR 
        if not os.path.exists(session_folder):
            os.mkdir(session_folder)
        if not os.path.exists(clip_frames_folder):
            os.mkdir(clip_frames_folder)
        if not os.path.exists(rendered_frames_folder):
            os.mkdir(rendered_frames_folder)

    def render_full_media(self, clip):
        print("AbstractContainerActionObject.render_full_media not impl")

    def render_clip_length_media(self, clip):
        print("AbstractContainerActionObject.render_clip_length_media not impl")
        
    def get_session_dir(self):
        return self.get_container_clips_dir() + "/" + self.get_container_program_id()

    def get_rendered_media_dir(self):
        if self.container_data.render_data.save_internally == True:
            return self.get_session_dir() + gmicheadless.RENDERED_FRAMES_DIR
        else:
            return self.container_data.render_data.render_dir + gmicheadless.RENDERED_FRAMES_DIR
            
    def get_container_program_id(self):
        id_md_str = str(self.container_data.container_clip_uid) + str(self.container_data.container_type) + self.container_data.program + self.container_data.unrendered_media #
        return hashlib.md5(id_md_str.encode('utf-8')).hexdigest() 

    def get_job_proxy(self):
        job_proxy = jobs.JobProxy(self.get_container_program_id())
        job_proxy.type = jobs.CONTAINER_CLIP_RENDER
        return job_proxy

    def get_container_clips_dir(self):
        return userfolders.get_data_dir() + appconsts.CONTAINER_CLIPS_DIR

    def add_as_status_polling_object(self):
        global _status_polling_thread
        if _status_polling_thread == None:
            _status_polling_thread = ContainerStatusPollingThread()
            _status_polling_thread.start()
                   
        _status_polling_thread.poll_objects.append(self)

    def remove_as_status_polling_object(self):
        _status_polling_thread.poll_objects.remove(self)

    def get_lowest_numbered_file(self):
        
        frames_info = gmicplayer.FolderFramesInfo(self.get_rendered_media_dir())
        lowest = frames_info.get_lowest_numbered_file()
        highest = frames_info.get_highest_numbered_file()
        return frames_info.get_lowest_numbered_file()

    def get_rendered_frame_sequence_resource_path(self):
        frame_file = self.get_lowest_numbered_file() # Works for both external and internal
        if frame_file == None:
            # Something is quite wrong.
            print("No frame file found for container clip at:", self.get_rendered_media_dir())
            return None

        resource_name_str = utils.get_img_seq_resource_name(frame_file, True)
        return self.get_rendered_media_dir() + "/" + resource_name_str
                
    def get_rendered_thumbnail(self):
        print("AbstractContainerActionObject.get_rendered_thumbnail not impl")
        return None
    
    def update_render_status(self):
        print("AbstractContainerActionObject.update_render_status not impl")

    def abort_render(self):
        print("AbstractContainerActionObject.abort_render not impl")


    def set_video_endoding(self, clip):
        current_profile_index = mltprofiles.get_profile_index_for_profile(current_sequence().profile)
        # These need to re-initialized always when using this module.
        toolsencoding.create_widgets(current_profile_index, True, True)
        toolsencoding.widgets.file_panel.enable_file_selections(False)

        # Create default render data if not available, we need to know profile to do this.
        if self.container_data.render_data == None:
            self.container_data.render_data = toolsencoding.create_container_clip_default_render_data_object(current_sequence().profile)
            
        encoding_panel = toolsencoding.get_encoding_panel(self.container_data.render_data, True)

        if self.container_data.render_data == None and toolsencoding.widgets.file_panel.movie_name.get_text() == "movie":
            toolsencoding.widgets.file_panel.movie_name.set_text("_gmic")

        align = dialogutils.get_default_alignment(encoding_panel)
        
        dialog = Gtk.Dialog(_("Container Clip Render Settings"),
                            gui.editor_window.window,
                            Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                            (_("Cancel"), Gtk.ResponseType.REJECT,
                             _("Set Encoding"), Gtk.ResponseType.ACCEPT))
        dialog.vbox.pack_start(align, True, True, 0)
        dialogutils.set_outer_margins(dialog.vbox)
        dialog.set_resizable(False)

        dialog.connect('response', self.encode_settings_callback)
        dialog.show_all()

    def encode_settings_callback(self, dialog, response_id):
        if response_id == Gtk.ResponseType.ACCEPT:
            _render_data = toolsencoding.get_render_data_for_current_selections()
            self.container_data.render_data = _render_data

        dialog.destroy()
        
    def clone_clip(self, old_clip):

        new_container_data = copy.deepcopy(old_clip.container_data)
        new_container_data.generate_clip_id()
        
        new_clip_action_object = get_action_object(new_container_data)
        new_clip_action_object.create_data_dirs_if_needed()
        new_clip = new_clip_action_object.create_container_clip_media_clone(old_clip)
        new_clip.container_data = new_container_data
        return new_clip

    def create_container_clip_media_clone(self, container_clip):
        self.show_cloning_clip_info()
        
        container_clip_action_object = get_action_object(container_clip.container_data)
        if container_clip.container_data.rendered_media == None:
            clone_clip = current_sequence().create_file_producer_clip(container_clip.path, None, False, container_clip.ttl)
        elif container_clip.container_data.render_data.do_video_render == True:
            # we have rendered a video clip for media last. 
            old_clip_path = container_clip_action_object.get_session_dir() + "/" + appconsts.CONTAINER_CLIP_VIDEO_CLIP_NAME + container_clip.container_data.render_data.file_extension
            new_clip_path = self.get_session_dir() + "/" + appconsts.CONTAINER_CLIP_VIDEO_CLIP_NAME + container_clip.container_data.render_data.file_extension
            shutil.copyfile(old_clip_path, new_clip_path)
            clone_clip =  current_sequence().create_file_producer_clip(new_clip_path, None, False, container_clip.ttl)
            
        else:
            # we have rendered a frame sequence clip for media last.
            old_frames_dir = container_clip_action_object.get_session_dir() + appconsts.CC_RENDERED_FRAMES_DIR
            new_frames_dir = self.get_session_dir() + appconsts.CC_RENDERED_FRAMES_DIR
            shutil.copytree(old_frames_dir, new_frames_dir)
        
            resource_path = self.get_rendered_frame_sequence_resource_path()
            clone_clip =  current_sequence().create_file_producer_clip(resource_path, None, False, container_clip.ttl)

        self.info_dialog.destroy()
        return clone_clip

    def show_cloning_clip_info(self):
        self.info_dialog = dialogutils.no_button_dialog("", Gtk.Label("Cloning Contaoner Clip Media"))


class GMicContainerActions(AbstractContainerActionObject):

    def __init__(self, container_data):
        AbstractContainerActionObject.__init__(self, container_data)
        self.render_type = -1 # to be set in methods below
        self.clip = None # to be set in methods below
        
    def render_full_media(self, clip):
        self.render_type = FULL_RENDER
        self.clip = clip
        self._launch_render(clip, 0, self.container_data.unrendered_length, 0)

        self.add_as_status_polling_object()

    def render_clip_length_media(self, clip):
        self.render_type = CLIP_LENGTH_RENDER
        self.clip = clip
        self._launch_render(clip, clip.clip_in, clip.clip_out + 1, clip.clip_in)

        self.add_as_status_polling_object()

    def _launch_render(self, clip, range_in, range_out, gmic_frame_offset):
        self.create_data_dirs_if_needed()

        gmicheadless.clear_flag_files(self.get_container_program_id())
    
        # We need data to be available for render process, 
        # create video_render_data object with default values if not available.
        if self.container_data.render_data == None:
            self.container_data.render_data = toolsencoding.create_container_clip_default_render_data_object(current_sequence().profile)
            
        gmicheadless.set_render_data(self.get_container_program_id(), self.container_data.render_data)
        
        job_proxy = self.get_job_proxy()
        job_proxy.text = _("Render Starting..")
        jobs.add_job(job_proxy)
        
        args = ("session_id:" + self.get_container_program_id(), 
                "script:" + self.container_data.program,
                "clip_path:" + self.container_data.unrendered_media,
                "range_in:" + str(range_in),
                "range_out:"+ str(range_out),
                "profile_desc:" + PROJECT().profile.description().replace(" ", "_"),
                "gmic_frame_offset:" + str(gmic_frame_offset))

        # Run with nice to lower priority if requested (currently hard coded to lower)
        nice_command = "nice -n " + str(10) + " " + respaths.LAUNCH_DIR + "flowbladegmicheadless"
        for arg in args:
            nice_command += " "
            nice_command += arg

        subprocess.Popen([nice_command], shell=True)

    def update_render_status(self):
        
        Gdk.threads_enter()
                    
        if gmicheadless.session_render_complete(self.get_container_program_id()) == True:
            self.remove_as_status_polling_object()

            # Using frame sequence as clip
            if  self.container_data.render_data.do_video_render == False:
                resource_path = self.get_rendered_frame_sequence_resource_path()
                if resource_path == None:
                    return # TODO: User info?

                rendered_clip = current_sequence().create_file_producer_clip(resource_path, new_clip_name=None, novalidate=False, ttl=1)
                
            # Using video clip as clip
            else:
                if self.container_data.render_data.save_internally == True:
                    resource_path = self.get_session_dir() +  "/" + gmicheadless.INTERNAL_CLIP_FILE + self.container_data.render_data.file_extension
                else:
                    resource_path = self.container_data.render_data.render_dir +  "/" + self.container_data.render_data.file_name + self.container_data.render_data.file_extension
                print("clip", resource_path)
                rendered_clip = current_sequence().create_file_producer_clip(resource_path, new_clip_name=None, novalidate=False, ttl=1)
            
            track, clip_index = current_sequence().get_track_and_index_for_id(self.clip.id)
            
            # Check if container clip still on timeline
            if track == None:
                # clip was removed from timeline
                # TODO: infowindow?
                return
            
            # Do replace edit
            data = {"old_clip":self.clip,
                    "new_clip":rendered_clip,
                    "rendered_media_path":resource_path,
                    "track":track,
                    "index":clip_index}
                    
            if self.render_type == FULL_RENDER: # unrendered -> fullrender
                action = edit.container_clip_full_render_replace(data)
                action.do_edit()
            else:  # unrendered -> clip render
                action = edit.container_clip_clip_render_replace(data)
                action.do_edit()
                
        else:
            status = gmicheadless.get_session_status(self.get_container_program_id())
            if status != None:
                step, frame, length, elapsed = status
                steps_count = 3
                if  self.container_data.render_data.do_video_render == False:
                    steps_count = 2
                msg = _("Step ") + str(step) + " / " + str(steps_count) + " - "
                if step == "1":
                    msg += _("Writing Clip Frames")
                elif step == "2":
                     msg += _("Rendering G'Mic Script")
                else:
                     msg += _("Encoding Video")
                     
                job_proxy = self.get_job_proxy()
                job_proxy.progress = float(frame)/float(length)
                job_proxy.elapsed = float(elapsed)
                job_proxy.text = msg
                
                jobs.show_message(job_proxy)
            else:
                print("Miss")

        Gdk.threads_leave()

    def abort_render(self):
        print("AbstractContainerActionObject.abort_render not impl")

    def create_icon(self):
        icon_path, length, info = _write_thumbnail_image(PROJECT().profile, self.container_data.unrendered_media)
        cr, surface = _create_image_surface(icon_path)
        cr.rectangle(0, 0, appconsts.THUMB_WIDTH, appconsts.THUMB_HEIGHT)
        cr.set_source_rgba(*OVERLAY_COLOR)
        cr.fill()
        type_icon = _get_type_icon(appconsts.CONTAINER_CLIP_GMIC)
        cr.set_source_surface(type_icon, 1, 30)
        cr.set_operator (cairo.OPERATOR_OVERLAY)
        cr.paint_with_alpha(0.5)
 
        return (surface, length)
        
    def get_rendered_thumbnail(self):
        surface, length = self.create_icon()
        return surface


class ContainerStatusPollingThread(threading.Thread):
    
    def __init__(self):
        self.poll_objects = []
        self.abort = False
        #self.running = False
        threading.Thread.__init__(self)

    def run(self):
        #self.running = True
        
        while self.abort == False:
            for poll_obj in self.poll_objects:
                poll_obj.update_render_status() # make sure poll objects enter Gtk threads
                    
                
            time.sleep(1.0)
            

    def shutdown(self):
        for poll_obj in self.poll_objects:
            poll_obj.abort_render()
        
        self.abort = True
        
