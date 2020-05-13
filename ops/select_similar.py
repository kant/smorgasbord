import bpy
from bpy_extras import object_utils
import bgl
import gpu
from gpu_extras.batch import batch_for_shader
from math import ceil, sqrt
import numpy as np

from smorgasbord.common.io import get_scalars, get_vecs, get_bounds_and_center
from smorgasbord.common.transf import transf_vecs
from smorgasbord.common.decorate import register
from smorgasbord.common.mesh_manip import add_geom_to_bmesh


def sample_surf(mesh, samplecnt=1024):
    """
    Draw N random samples on the surface of a triangle mesh.

    Parameters
    ----------
    mesh : bpy.types.Mesh
        Blender triangle mesh to sample from.
    samplecnt : int = 1024
        Number of samples to draw.

    Returns
    -------
    out : numpy.ndarray
        2D array with shape (N, 3), containing the coordinates of the
        N drawn sample points.
    """
    # Accumulate all triangle areas in the mesh to sample each triangle 
    # with a probability proportional to its surface area.
    areas = get_scalars(mesh.polygons, 'area', np.float64)
    areas = np.cumsum(areas)
    # Choose N random floats between 0 and the sum of all areas.
    rdareas = np.random.uniform(0., areas[-1], samplecnt)
    # For each random float, find the index of the triangle with the
    # highest, but less equal cumulative area (the left neighbor of the
    # randomly drawn area).
    rdindcs = np.searchsorted(areas, rdareas)
    # These vectorized calculations eat up a lot of memory. Make some
    # unneeded data applicable for garbage collection.
    del areas, rdareas

    # Get the vertex coordinates.
    pts = get_vecs(mesh.vertices)
    # Get the vertex indices for each triangle.
    tris = get_vecs(mesh.polygons, 'vertices', dtype=np.int32)
    # Inner indexing operation: For each randomly chosen triangle index,
    # insert the actual vertex indices of the corresponding triangle
    # into the array.
    # Outer indexing operation: For each vertex index in the triangle
    # array, insert its actual vertex coordinates.
    # This 3D array holds a lot of redundant data now, this probably
    # scales pretty badly with the sample count and the number of
    # triangles.
    tris = pts[tris[rdindcs,:]]
    del rdindcs, pts

    # For each sample, draw two random floats that determine where on
    # the triangle the sample point is placed.
    # This is done via the following formula, with the triangle's
    # vertex coordinate vectors A, B, C:
    # P = (1 - sqrt(r1))*A + sqrt(r1)*(1 - r2)*B + sqrt(r1)*r2*C
    r1 = np.sqrt(np.random.rand(samplecnt))
    r2 = np.random.rand(samplecnt)
    # Calculate coefficients for each vertex A, B, C
    coef = np.stack(
        (1 - r1, r1 * (1 - r2), r1 * r2),
        axis=1,
        ).reshape(-1, 3, 1)
    del r1, r2
    # For each triangle sum up A, B, and C, leaving one point per
    # sample. Remember, one triangle can be several times in 'tris',
    # but a different sample is drawn from its surface every time.
    return np.sum(coef * tris, axis=1)


def get_shape_distrib(mesh, samplecnt=1024, bincnt=32):
    pts = sample_surf(mesh, samplecnt)
    # k=1 eliminates diagonal indices
    i, j = np.triu_indices(samplecnt, k=1)
    return np.histogram(
        np.linalg.norm(pts[i] - pts[j], axis=1),
        bins=bincnt,
        density=True,
        )


def create_debug_mesh(context, pts):
    import bmesh as bm
    bob = bm.new()
    add_geom_to_bmesh(bob, pts, [])
    mesh = bpy.data.meshes.new("Samples")
    bob.to_mesh(mesh)
    mesh.update()
    object_utils.object_data_add(context, mesh)


def save_barplot(xvals, yvals, barwidth, xmax, title, filename):
    import matplotlib.pyplot as plt
    plt.clf()
    # plt.xlim(right=xmax)
    plt.title(title)
    plt.bar(xvals, yvals, barwidth)
    plt.savefig(filename + ".png")


def draw_points(pts):
    shader = gpu.shader.from_builtin('3D_UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'POINTS', {"pos": tuple(pts)})
    shader.bind()
    shader.uniform_float("color", (0.8, 0.3, 0.4, 1))
    batch.draw(shader)


@register
class SelectSimilar(bpy.types.Operator):
    bl_idname = "select.select_similar"
    bl_label = "Select Similar"
    bl_description = ""
    bl_options = {'REGISTER', 'UNDO'}
    menus = [bpy.types.VIEW3D_MT_select_object]

    def _get_sim_limits(self):
        return SelectSimilar._sim_limits

    def _set_sim_limits(self, val):
        # Clamp min to max
        SelectSimilar._sim_limits = (min(val), val[1])

    def _get_samplecnt(self):
        return SelectSimilar._samplecnt

    def _set_samplecnt(self, val):
        SelectSimilar._samplecnt = val
        SelectSimilar.bincnt = ceil(sqrt(val))

    _sim_limits = (0, 1)
    sim_limits: bpy.props.FloatVectorProperty(
        name="Similarity limits",
        description=(
            "Select objects whose shape differs more than min and less "
            "than max from the active objects' shape"
        ),
        size=2,
        step=10,
        default=(0, 1),
        min=0,
        get=_get_sim_limits,
        set=_set_sim_limits,
    )
    _samplecnt = 512
    samplecnt: bpy.props.IntProperty(
        name="Sample count",
        description=(
            "Number of samples taken to compute a simple shape "
            "representation for every object to be compared. More "
            "samples improve accuracy, at the expense of computation "
            "time"
        ),
        default=1024,
        min=16,
        max=16384,
        soft_min=64,
        soft_max=1024,
        step=2,
        set=_set_samplecnt,
        get=_get_samplecnt,
    )
    bincnt = 32
    handle = None
    svals = {}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
            and context.object is not None
            and context.object.type == 'MESH'
            )

    def _remove_handle(self):
        if self.handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(
                self.handle, 'WINDOW')

    def _draw_samples(self, context, pts):
        if context.area.type != 'VIEW_3D':
            self.report({'WARNING'},
                "Samples can only be drawn in the 3D View")
            return

        self._remove_handle()
        self.handle = bpy.types.SpaceView3D.draw_handler_add(
            draw_points,
            (pts,),
            'WINDOW',
            'POST_VIEW',
            )
        context.area.tag_redraw()

    def _save_bar_plot(self, ob, xvals, yvals):
        bounds, _ = get_bounds_and_center(ob.bound_box)
        maxdist = np.linalg.norm(bounds)
        save_barplot(
            xvals=xvals,
            yvals=yvals[:-1],
            barwidth=maxdist / self.bincnt,
            xmax=maxdist,
            title=(
                f"Samples: {self._samplecnt}, "
                f"Bins: {self.bincnt}, "
                f"Diaglength: {maxdist}"
            ),
            filename=ob.name,
            )

    def _comp_shape_distribs(self, context):
        self.svals.clear()
        all_type_err = True  # no obj is of type mesh
        ob = context.object
        ahist, abins = get_shape_distrib(
            ob.data,
            self._samplecnt,
            self.bincnt,
        )
        self._save_bar_plot(ob, ahist, abins)

        # Compare only selection if selection exists, compare all
        # objects in the active collection if not.
        selobs = context.selected_objects
        if len(selobs) < 2:
            selobs = context.collection.objects
        for o in selobs:
            if o.type != 'MESH' or o is ob:
                continue

            all_type_err = False
            ohist, obins = get_shape_distrib(
                o.data,
                self._samplecnt,
                self.bincnt,
            )
            self._save_bar_plot(o, ohist, obins)
            self.svals[o.name] = np.linalg.norm(ohist - ahist, ord=1)

        if all_type_err:
            self.report({'ERROR_INVALID_INPUT'},
                        "An object must be of type mesh")
            return False
        return True

    def invoke(self, context, event):
        if self._comp_shape_distribs(context):
            return self.execute(context)
        else:
            return {'CANCELLED'}

    def execute(self, context):
        if not self.svals:
            self.report({'ERROR_INVALID_INPUT'}, "Sample first")
            return {'CANCELLED'}

        mins, maxs = self.sim_limits
        for name, simval in self.svals.items():
            bpy.data.objects[name].select_set(mins <= simval < maxs)
        return {'FINISHED'}


if __name__ == "__main__":
    register()