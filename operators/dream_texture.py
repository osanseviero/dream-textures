import sys
import bpy
import os
import hashlib
import numpy as np
from numpy.typing import NDArray
from multiprocessing.shared_memory import SharedMemory

from ..property_groups.dream_prompt import pipeline_options

from ..preferences import StableDiffusionPreferences
from ..pil_to_image import *
from ..prompt_engineering import *
from ..absolute_path import WEIGHTS_PATH, CLIPSEG_WEIGHTS_PATH
from ..generator_process import Generator
from ..generator_process.actions.prompt_to_image import Pipeline, Optimizations, ImageGenerationResult

import tempfile

generator_advance = None
last_data_block = None
timer = None

def save_temp_image(img, path=None):
    path = path if path is not None else tempfile.NamedTemporaryFile().name

    settings = bpy.context.scene.render.image_settings
    file_format = settings.file_format
    mode = settings.color_mode
    depth = settings.color_depth

    settings.file_format = 'PNG'
    settings.color_mode = 'RGBA'
    settings.color_depth = '8'

    img.save_render(path)

    settings.file_format = file_format
    settings.color_mode = mode
    settings.color_depth = depth

    return path

def bpy_image(name, width, height, pixels):
    image = bpy.data.images.new(name, width=width, height=height)
    image.pixels[:] = pixels
    image.pack()
    return image

class DreamTexture(bpy.types.Operator):
    bl_idname = "shade.dream_texture"
    bl_label = "Dream Texture"
    bl_description = "Generate a texture with AI"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return Generator.shared().can_use()

    def execute(self, context):
        history_entries = []
        is_file_batch = context.scene.dream_textures_prompt.prompt_structure == file_batch_structure.id
        file_batch_lines = []
        if is_file_batch:
            context.scene.dream_textures_prompt.iterations = 1
            file_batch_lines = [line for line in context.scene.dream_textures_prompt_file.lines if len(line.body.strip()) > 0]
            history_entries = [context.preferences.addons[StableDiffusionPreferences.bl_idname].preferences.history.add() for _ in file_batch_lines]
        else:
            history_entries = [context.preferences.addons[StableDiffusionPreferences.bl_idname].preferences.history.add() for _ in range(context.scene.dream_textures_prompt.iterations)]
        for i, history_entry in enumerate(history_entries):
            for prop in context.scene.dream_textures_prompt.__annotations__.keys():
                try:
                    if hasattr(history_entry, prop):
                        setattr(history_entry, prop, getattr(context.scene.dream_textures_prompt, prop))
                except:
                    continue
            if is_file_batch:
                history_entry.prompt_structure = custom_structure.id
                history_entry.prompt_structure_token_subject = file_batch_lines[i].body

        node_tree = context.material.node_tree if hasattr(context, 'material') and hasattr(context.material, 'node_tree') else None
        screen = context.screen
        scene = context.scene

        generated_args = scene.dream_textures_prompt.generate_args()

        init_image = None
        if generated_args['use_init_img']:
            match generated_args['init_img_src']:
                case 'file':
                    init_image = save_temp_image(scene.init_img)
                case 'open_editor':
                    for area in screen.areas:
                        if area.type == 'IMAGE_EDITOR':
                            if area.spaces.active.image is not None:
                                init_image = save_temp_image(area.spaces.active.image)

        # Setup the progress indicator
        def step_progress_update(self, context):
            if hasattr(context.area, "regions"):
                for region in context.area.regions:
                    if region.type == "UI":
                        region.tag_redraw()
            return None
        bpy.types.Scene.dream_textures_progress = bpy.props.IntProperty(name="", default=0, min=0, max=generated_args['steps'], update=step_progress_update)
        scene.dream_textures_info = "Starting..."

        last_data_block = None
        def step_callback(_, step_image: ImageGenerationResult):
            nonlocal last_data_block
            if last_data_block is not None:
                bpy.data.images.remove(last_data_block)
                last_data_block = None
            if step_image.final:
                return
            scene.dream_textures_progress = step_image.step
            if step_image.image is not None:
                last_data_block = bpy_image(f"Step {step_image.step}/{generated_args['steps']}", step_image.image.shape[1], step_image.image.shape[0], step_image.image.ravel())
                for area in screen.areas:
                    if area.type == 'IMAGE_EDITOR':
                        area.spaces.active.image = last_data_block

        iteration = 0
        def done_callback(future):
            nonlocal last_data_block
            nonlocal iteration
            if hasattr(gen, '_active_generation_future'):
                del gen._active_generation_future
            image_result: ImageGenerationResult | list = future.result()
            if isinstance(image_result, list):
                image_result = image_result[-1]
            if last_data_block is not None:
                bpy.data.images.remove(last_data_block)
                last_data_block = None
            image = bpy_image(str(image_result.seed), image_result.image.shape[1], image_result.image.shape[0], image_result.image.ravel())
            if node_tree is not None:
                nodes = node_tree.nodes
                texture_node = nodes.new("ShaderNodeTexImage")
                texture_node.image = image
                nodes.active = texture_node
            for area in screen.areas:
                if area.type == 'IMAGE_EDITOR':
                    area.spaces.active.image = image
            scene.dream_textures_prompt.seed = str(image_result.seed) # update property in case seed was sourced randomly or from hash
            # create a hash from the Blender image datablock to use as unique ID of said image and store it in the prompt history
            # and as custom property of the image. Needs to be a string because the int from the hash function is too large
            image_hash = hashlib.sha256((np.array(image.pixels) * 255).tobytes()).hexdigest()
            image['dream_textures_hash'] = image_hash
            scene.dream_textures_prompt.hash = image_hash
            history_entries[iteration].seed = str(image_result.seed)
            history_entries[iteration].random_seed = False
            history_entries[iteration].hash = image_hash
            iteration += 1
            if iteration < generated_args['iterations']:
                generate_next()
            else:
                scene.dream_textures_info = ""
                scene.dream_textures_progress = 0

        def exception_callback(_, exception):
            scene.dream_textures_info = ""
            scene.dream_textures_progress = 0
            if hasattr(gen, '_active_generation_future'):
                del gen._active_generation_future
            self.report({'ERROR'}, str(exception))
            raise exception

        gen = Generator.shared()
        def generate_next():
            if init_image is not None:
                match generated_args['init_img_action']:
                    case 'modify':
                        f = gen.image_to_image(
                            image=init_image,
                            **generated_args
                        )
                    case 'inpaint':
                        f = gen.inpaint(
                            image=init_image,
                            **generated_args
                        )
                    case 'outpaint':
                        raise NotImplementedError()
            else:
                f = gen.prompt_to_image(
                    **generated_args,
                )
            gen._active_generation_future = f
            f.call_done_on_exception = False
            f.add_response_callback(step_callback)
            f.add_exception_callback(exception_callback)
            f.add_done_callback(done_callback)
        generate_next()
        return {"FINISHED"}

def kill_generator(context=bpy.context):
    Generator.shared_close()
    try:
        context.scene.dream_textures_info = ""
        context.scene.dream_textures_progress = 0
    except:
        pass

class ReleaseGenerator(bpy.types.Operator):
    bl_idname = "shade.dream_textures_release_generator"
    bl_label = "Release Generator"
    bl_description = "Releases the generator class to free up VRAM"
    bl_options = {'REGISTER'}

    def execute(self, context):
        kill_generator(context)
        return {'FINISHED'}

class CancelGenerator(bpy.types.Operator):
    bl_idname = "shade.dream_textures_stop_generator"
    bl_label = "Cancel Generator"
    bl_description = "Stops the generator without reloading everything next time"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        gen = Generator.shared()
        return hasattr(gen, "_active_generation_future") and gen._active_generation_future is not None and not gen._active_generation_future.cancelled and not gen._active_generation_future.done

    def execute(self, context):
        gen = Generator.shared()
        gen._active_generation_future.cancel()
        context.scene.dream_textures_info = ""
        context.scene.dream_textures_progress = 0
        return {'FINISHED'}
