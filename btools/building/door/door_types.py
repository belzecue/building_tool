import bmesh

from ..arch import (
    fill_arch,
    create_arch,
    add_arch_depth,
)
from ..array import spread_array, clamp_array_count, get_array_split_edges
from ..fill import fill_face
from ..frame import add_frame_depth
from ..materialgroup import (
    MaterialGroup,
    map_new_faces,
    add_faces_to_group,
    find_faces_without_matgroup,
)
from ...utils import (
    clamp,
    XYDir,
    VEC_UP,
    validate,
    local_xyz,
    sort_verts,
    valid_ngon,
    ngon_to_quad,
    get_top_edges,
    get_top_faces,
    get_bottom_faces,
    extrude_face_region,
    calc_face_dimensions,
    subdivide_face_vertically,
    subdivide_face_horizontally,
)


def create_door(bm, faces, prop):
    """Create door from face selection"""
    for face in faces:
        face.select = False
        if not valid_ngon(face):
            ngon_to_quad(bm, face)

        clamp_array_count(face, prop)
        array_faces = subdivide_face_horizontally(
            bm, face, widths=[prop.width] * prop.count
        )
        max_width = calc_face_dimensions(array_faces[0])[0]

        split_edges = get_array_split_edges(array_faces)
        split_faces = [create_door_split(bm, aface, prop) for aface in array_faces]
        spread_array(bm, split_edges, split_faces, max_width, prop)

        for face in split_faces:
            door, arch = create_door_frame(bm, face, prop)
            create_door_fill(bm, door, prop)
            if prop.add_arch:
                fill_arch(bm, arch, prop)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)

    nulfaces = find_faces_without_matgroup(bm)
    add_faces_to_group(bm, nulfaces, MaterialGroup.WALLS)
    return True


@map_new_faces(MaterialGroup.WALLS)
def create_door_split(bm, face, prop):
    """Use properties from SizeOffset to subdivide face into regular quads"""
    wall_w, wall_h = calc_face_dimensions(face)
    width, height, offset = *prop.size, prop.offset
    # horizontal split
    h_widths = [
        wall_w / 2 - offset.x - width / 2,
        width,
        wall_w / 2 + offset.x - width / 2,
    ]
    h_faces = subdivide_face_horizontally(bm, face, h_widths)
    # vertical split
    v_width = [height, wall_h - height]
    v_faces = subdivide_face_vertically(bm, h_faces[1], v_width)

    return v_faces[0]


def create_door_frame(bm, face, prop):
    """Extrude and inset face to make door frame"""
    normal = face.normal.copy()

    # XXX Frame thickness should not exceed size of door
    min_frame_size = min(calc_face_dimensions(face)) / 2
    prop.frame_thickness = clamp(prop.frame_thickness, 0.01, min_frame_size - 0.001)

    door_face, frame_faces = make_door_inset(bm, face, prop)
    arch_face = None

    # create arch
    if prop.add_arch:
        frame_faces.remove(
            get_top_faces(frame_faces).pop()
        )  # remove top face from frame_faces
        top_edges = get_top_edges(
            {e for f in get_bottom_faces(frame_faces, n=2) for e in f.edges}, n=2
        )
        arch_face, arch_frame_faces = create_arch(
            bm, top_edges, frame_faces, prop.arch, prop.frame_thickness, local_xyz(face)
        )
        frame_faces += arch_frame_faces
    else:
        # -- postprocess merge loose split verts
        merge_loose_split_verts(bm, door_face, prop)

    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))

    # add depths
    if prop.add_arch:
        [door_face], _, [arch_face], frame_faces = add_frame_depth(
            bm, [door_face], [], [arch_face], frame_faces, prop.frame_depth, normal
        )
        arch_face, new_frame_faces = add_arch_depth(
            bm, arch_face, prop.arch.depth, normal
        )
        frame_faces += new_frame_faces
    else:
        [door_face], _, _, frame_faces = add_frame_depth(
            bm, [door_face], [], [], frame_faces, prop.frame_depth, normal
        )

    door_face, new_frame_faces = add_door_depth(bm, door_face, prop.door_depth, normal)
    frame_faces += new_frame_faces

    # add face maps
    add_faces_to_group(bm, [door_face], MaterialGroup.DOOR)
    add_faces_to_group(bm, validate(frame_faces), MaterialGroup.FRAME)
    if prop.add_arch:
        add_faces_to_group(bm, [arch_face], MaterialGroup.DOOR)

    return door_face, arch_face


def add_door_depth(bm, door, depth, normal):
    if depth > 0.0:
        door_faces, frame_faces = extrude_face_region(bm, [door], -depth, normal)
        return door_faces[0], frame_faces
    else:
        return door, []


def create_door_fill(bm, face, prop):
    """Add decorative elements on door face"""
    if prop.double_door:
        faces = subdivide_face_horizontally(bm, face, widths=[1, 1])
        for f in faces:
            fill_face(bm, f, prop, "DOOR")
    else:
        fill_face(bm, face, prop, "DOOR")


def make_door_inset(bm, face, prop):
    """Make one horizontal cut and two vertical cuts on face"""
    width, frame_thickness = prop.width, prop.frame_thickness

    door_width = width - frame_thickness * 2
    _, face_height = calc_face_dimensions(face)
    door_height = face_height - frame_thickness
    # horizontal cuts
    h_widths = [frame_thickness, door_width, frame_thickness]
    h_faces = subdivide_face_horizontally(bm, face, h_widths)
    # vertical cuts
    v_widths = [door_height, frame_thickness]
    v_faces = subdivide_face_vertically(bm, h_faces[1], v_widths)
    return v_faces[0], h_faces[::2] + [v_faces[1]]


def merge_loose_split_verts(bm, door_face, prop):
    """Merge the split verts to the corners of the window frame"""
    median = door_face.calc_center_median()
    door_face_verts = sort_verts(door_face.verts, VEC_UP)[2:]
    for vert in door_face_verts:
        extent_edge = [e for e in vert.link_edges if e not in door_face.edges].pop()
        corner_vert = extent_edge.other_vert(vert)

        move_mag = prop.frame_thickness
        move_dir = XYDir(corner_vert.co - median)
        bmesh.ops.translate(bm, verts=[corner_vert], vec=move_dir * move_mag)
