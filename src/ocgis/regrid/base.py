import logging
from copy import deepcopy
from types import NoneType

import ESMF
import numpy as np
from ESMF.api.constants import RegridMethod

from ocgis import constants
from ocgis import env
from ocgis.base import AbstractOcgisObject, get_dimension_names
from ocgis.collection.field import Field
from ocgis.constants import DMK
from ocgis.exc import RegriddingError, CornersInconsistentError
from ocgis.spatial.grid import Grid, expand_grid
from ocgis.spatial.spatial_subset import SpatialSubsetOperation
from ocgis.util.broadcaster import broadcast_scope, broadcast_variable
from ocgis.util.helpers import get_esmf_corners_from_ocgis_corners, create_ocgis_corners_from_esmf_corners
from ocgis.util.logging_ocgis import ocgis_lh
from ocgis.variable.base import Variable
from ocgis.variable.crs import Spherical, create_crs


class RegridOperation(AbstractOcgisObject):
    """
    Execute a regrid operation handling spatial subsetting and coordinate system transformations.

    :param field_src: The source field to regrid.
    :type field_src: :class:`ocgis.Field`
    :param field_dst: The destination regrid field.
    :type field_dst: :class:`ocgis.Field`
    :param subset_field: If provided, use this field to subset the regridding fields.
    :type subset_field: :class:`ocgis.Field`
    :param regrid_options: A dictionary of keyword options to pass to :func:`~ocgis.regrid.base.regrid_field`.
    :type regrid_options: dict
    :param bool revert_dst_crs: If ``True``, revert the destination grid coordinate system if it needed to be
     transformed. Typically, a number of source fields are regridded to a common destination and this transform
     should only occur once.
    """

    def __init__(self, field_src, field_dst, subset_field=None, regrid_options=None, revert_dst_crs=False):
        assert isinstance(field_src, Field)
        assert isinstance(field_dst, Field)
        assert isinstance(subset_field, (Field, NoneType))

        self.field_dst = field_dst
        self.field_src = field_src
        self.subset_field = subset_field
        self.revert_dst_crs = revert_dst_crs
        if regrid_options is None:
            self.regrid_options = {}
        else:
            self.regrid_options = regrid_options

    def execute(self):
        """
        Execute regridding operation.

        :rtype: :class:`ocgis.Field`
        """

        regrid_destination, backtransform_dst_crs = self._get_regrid_destination_()
        regrid_source, backtransform_src_crs = self._get_regrid_source_()

        # Regrid the input field.
        ocgis_lh(logger='regrid', msg='Creating regridded field...', level=logging.INFO)
        regridded_source = regrid_field(regrid_source, regrid_destination, **self.regrid_options)

        if backtransform_src_crs is not None:
            regridded_source.update_crs(backtransform_src_crs)
            # self.field_src.update_crs(backtransform_src_crs)
        if backtransform_dst_crs is not None and self.revert_dst_crs:
            self.field_dst.update_crs(backtransform_dst_crs)

        return regridded_source

    def _get_regrid_destination_(self):
        """
        Prepare destination field for regridding.

        :rtype: (:class:`~ocgis.Field`, :class:`~ocgis.CoordinateReferenceSystem` or ``None``)
        """

        # Transform the coordinate system of the regrid destination. ###################################################

        # Update the regrid destination coordinate system must be updated to match the source.
        if self.field_dst.crs != Spherical():
            ocgis_lh(logger='regrid',
                     msg='updating regrid destination to spherical. regrid destination crs is: {}'.format(
                         self.field_dst.crs), level=logging.DEBUG)
            backtransform_crs = deepcopy(self.field_dst.crs)
            self.field_dst.update_crs(Spherical())
        else:
            backtransform_crs = None

        # Spatially subset the regrid destination. #####################################################################
        if self.subset_field is None:
            ocgis_lh(logger='regrid', msg='no spatial subsetting', level=logging.DEBUG)
            regrid_destination = self.field_dst
        else:
            ss = SpatialSubsetOperation(self.field_dst)
            regrid_destination = ss.get_spatial_subset('intersects', self.subset_field.geom,
                                                       use_spatial_index=env.USE_SPATIAL_INDEX,
                                                       select_nearest=False)

        return regrid_destination, backtransform_crs

    def _get_regrid_source_(self):
        # Update the source coordinate system to spherical.
        if self.field_src.crs != Spherical():
            ocgis_lh(logger='regrid',
                     msg='updating regrid source to spherical. regrid source crs is: {}'.format(
                         self.field_src.crs), level=logging.DEBUG)
            backtransform_crs = deepcopy(self.field_src.crs)
            self.field_src.update_crs(Spherical())
        else:
            backtransform_crs = None

        return self.field_src, backtransform_crs


def get_ocgis_grid_from_esmf_grid(egrid):
    """
    Create an OCGIS grid from an ESMF grid.

    :param egrid: The input ESMF grid to convert to an OCGIS grid.
    :type egrid: :class:`ESMF.Grid`
    :return: :class:`~ocgis.Grid`
    """

    dmap = egrid._ocgis['dimension_map']
    edims = list(egrid._ocgis['dimnames'])
    odims = egrid._ocgis['dimnames_backref']

    coords = egrid.coords[ESMF.StaggerLoc.CENTER]
    var_x = Variable(name=dmap.get_variable(DMK.X), value=coords[0], dimensions=edims)
    var_y = Variable(name=dmap.get_variable(DMK.Y), value=coords[1], dimensions=edims)

    # Build OCGIS corners array if corners are present on the ESMF grid object.
    has_corners = get_esmf_grid_has_corners(egrid)
    if has_corners:
        corner = egrid.coords[ESMF.StaggerLoc.CORNER]
        if egrid.periodic_dim == 0:
            xcorner = np.zeros([corner[0].shape[0] + 1, corner[0].shape[1]], dtype=corner[0].dtype)
            xcorner[0:corner[0].shape[0], :] = corner[0]
            xcorner[-1, :] = corner[0][0, :]

            ycorner = np.zeros([corner[1].shape[0] + 1, corner[1].shape[1]], dtype=corner[1].dtype)
            ycorner[0:corner[1].shape[0], :] = corner[1]
            ycorner[-1, :] = corner[1][0, :]
        else:
            xcorner = corner[0]
            ycorner = corner[1]
        ocorner_x = create_ocgis_corners_from_esmf_corners(xcorner)
        ocorner_y = create_ocgis_corners_from_esmf_corners(ycorner)

        cdims = deepcopy(edims)
        cdims.append(constants.DEFAULT_NAME_CORNERS_DIMENSION)
        vocorner_x = Variable(name=dmap.get_bounds(DMK.X), value=ocorner_x, dimensions=cdims)
        vocorner_y = Variable(name=dmap.get_bounds(DMK.Y), value=ocorner_y, dimensions=cdims)

    crs = get_crs_from_esmf(egrid)

    ogrid = Grid(x=var_x, y=var_y, crs=crs)

    # Does the grid have a mask?
    has_mask = False
    if egrid.mask is not None:
        if egrid.mask[ESMF.StaggerLoc.CENTER] is not None:
            has_mask = True
    if has_mask:
        # if there is a mask, update the grid values
        egrid_mask = egrid.mask[ESMF.StaggerLoc.CENTER]
        egrid_mask = np.invert(egrid_mask.astype(bool))
        ogrid.set_mask(egrid_mask)

    ogrid.parent.dimension_map = dmap

    if tuple(odims) != tuple(edims):
        broadcast_variable(var_x, odims)
        broadcast_variable(var_y, odims)
        if has_corners:
            broadcast_variable(vocorner_x, list(odims) + [constants.DEFAULT_NAME_CORNERS_DIMENSION])
            broadcast_variable(vocorner_y, list(odims) + [constants.DEFAULT_NAME_CORNERS_DIMENSION])

    if has_corners:
        var_x.set_bounds(vocorner_x)
        var_y.set_bounds(vocorner_y)

    return ogrid


def get_esmf_field_from_ocgis_field(ofield, esmf_field_name=None, **kwargs):
    """
    :param ofield: The OCGIS field to convert to an ESMF field.
    :type ofield: :class:`ocgis.Field`
    :param str esmf_field_name: An optional ESMF field name. If ``None``, use the name of the data variable on
     ``ofield``.
    :param dict kwargs: Any keyword arguments to :func:`ocgis.regrid.base.get_esmf_grid`.
    :return: An ESMF field object.
    :rtype: :class:`ESMF.api.field.Field`
    :raises: ValueError
    """

    # ESMF fields only support a single field value.
    if len(ofield.data_variables) > 1:
        msg = 'Only one data variable may be converted.'
        raise ValueError(msg)

    # It is possible to create an ESMF field w/out a target variable.
    if len(ofield.data_variables) == 1:
        target_variable = ofield.data_variables[0]
        if esmf_field_name is None:
            esmf_field_name = target_variable.name
    else:
        target_variable = None

    # Create the ESMF grid.
    egrid = get_esmf_grid(ofield.grid, **kwargs)

    egrid_dimnames = egrid._ocgis['dimnames']
    ndbounds = []
    if target_variable is not None:
        tv_dimnames = deepcopy(target_variable.dimension_names)
        if ofield.time is not None:
            time_dimname = ofield.time.dimensions[0].name
        else:
            time_dimname = None
        new_order = list(egrid_dimnames)

        for d in tv_dimnames:
            if d not in new_order and d != time_dimname:
                new_order.append(d)
                ndbounds.append(len(target_variable.dimensions_dict[d]))
        if time_dimname is not None:
            new_order.append(time_dimname)
            ndbounds.append(len(ofield.time.dimensions[0]))
    if len(ndbounds) == 0:
        ndbounds = None

    ####################################################################################################################
    # Choose the ESMF data type.

    if target_variable is not None:
        other = np.dtype(target_variable.dtype)
        if other == np.float32:
            other = np.float32
        elif other == np.float64:
            other = np.float64

        tks = {np.float32: ESMF.TypeKind.R4,
               np.float64: ESMF.TypeKind.R8}

        tk = tks[other]
    else:
        tk = ESMF.TypeKind.R4

    ####################################################################################################################

    efield = ESMF.Field(egrid, name=esmf_field_name, ndbounds=ndbounds, typekind=tk)
    efield._ocgis = {}
    efield._ocgis['dimension_map'] = deepcopy(ofield.dimension_map)
    efield._ocgis['ocgis_grid'] = ofield.grid

    if target_variable is not None:
        efield._ocgis['dimnames_backref'] = deepcopy(target_variable.dimensions)
        efield._ocgis['dimnames'] = tuple(new_order)
        with broadcast_scope(target_variable, new_order):
            efield.data[:] = target_variable.get_value()
    else:
        efield._ocgis['dimnames'] = egrid_dimnames

    return efield


def get_esmf_grid_has_corners(egrid):
    return egrid.has_corners


def get_ocgis_field_from_esmf_field(efield, field=None):
    """
    :param efield: The ESMPy field object to convert to an OCGIS field.
    :type efield: :class:`ESMF.Field`
    :param field: If provided, use this as the template field for OCGIS field creation.
    :type field: :class:`~ocgis.Field`
    :return: :class:`~ocgis.Field`
    """
    ometa = efield._ocgis
    dimnames = ometa.get('dimnames')
    dimnames_backref = ometa.get('dimnames_backref')

    ogrid = ometa.get('ocgis_grid')
    if ogrid is None:
        ogrid = get_ocgis_grid_from_esmf_grid(efield.grid)

    ovar = None
    if dimnames is not None and efield.name is not None:
        ovar = Variable(name=efield.name, value=efield.data, dimensions=dimnames, dtype=efield.data.dtype)
        broadcast_variable(ovar, get_dimension_names(dimnames_backref))
        ovar.set_dimensions(dimnames_backref, force=True)

    if field is None:
        field = Field(grid=ogrid)
    else:
        field.set_grid(ogrid)

    if ovar is not None:
        field.add_variable(ovar, is_data=True, force=True)

        if ogrid.has_mask:
            field.grid.set_mask(ogrid.get_mask(), cascade=True)

    return field


def get_crs_from_esmf(esmf_object):
    """
    Get the coordinate system associated with an ESMF object. Works with ESMF fields and grids.
    """
    mp = {ESMF.CoordSys.SPH_DEG: Spherical()}

    try:
        grid = esmf_object.grid
    except AttributeError:
        grid = esmf_object
    coord_sys = grid.coord_sys
    try:
        ret = mp[coord_sys]
    except KeyError:
        msg = 'ESMF coordinate system ("coord_sys") flag {} not supported.'.format(coord_sys)
        raise ValueError(msg)
    return ret


def get_esmf_grid(ogrid, regrid_method='auto', value_mask=None):
    """
    Create an ESMF :class:`~ESMF.driver.grid.Grid` object from an OCGIS :class:`ocgis.Grid` object.

    :param ogrid: The target OCGIS grid to convert to an ESMF grid.
    :type ogrid: :class:`~ocgis.Grid`
    :param regrid_method: If ``'auto'`` or :attr:`ESMF.api.constants.RegridMethod.CONSERVE`, use corners/bounds from the
     grid object. If :attr:`ESMF.api.constants.RegridMethod.CONSERVE`, the corners/bounds must be present on the grid.
    :param value_mask: If an ``bool`` :class:`numpy.array` with same shape as ``ogrid``, use a logical *or* operation
     with the OCGIS field mask when creating the input grid's mask. Values of ``True`` in ``value_mask`` are assumed to
     be masked. If ``None`` is provided (the default) then the mask will be set using the spatial mask on ``ogrid``.
    :type value_mask: :class:`numpy.array`
    :rtype: :class:`ESMF.driver.grid.Grid`
    """

    assert isinstance(ogrid, Grid)
    assert create_crs(ogrid.crs) == Spherical()

    pkwds = get_periodicity_parameters(ogrid)

    # Fill ESMPy grid coordinate values. ###############################################################################

    dimensions = ogrid.parent.dimensions
    get_dimension = ogrid.dimension_map.get_dimension
    y_dimension = get_dimension(DMK.Y, dimensions=dimensions)
    x_dimension = get_dimension(DMK.X, dimensions=dimensions)

    # ESMPy has index 0 = x-coordinate and index 1 = y-coordinate.
    max_index = np.array([x_dimension.size, y_dimension.size], dtype=np.int32)
    pkwds['coord_sys'] = ESMF.CoordSys.SPH_DEG
    pkwds['staggerloc'] = ESMF.StaggerLoc.CENTER
    pkwds['max_index'] = max_index
    egrid = ESMF.Grid(**pkwds)

    egrid._ocgis['dimnames'] = (x_dimension.name, y_dimension.name)
    egrid._ocgis['dimnames_backref'] = get_dimension_names(ogrid.dimensions)
    egrid._ocgis['dimension_map'] = deepcopy(ogrid.dimension_map)

    ogrid_dimnames = (y_dimension.name, x_dimension.name)
    is_yx_order = get_dimension_names(ogrid.dimensions) == ogrid_dimnames

    ovalue_stacked = ogrid.get_value_stacked()
    row = egrid.get_coords(1, staggerloc=ESMF.StaggerLoc.CENTER)
    orow = ovalue_stacked[0, ...]
    if is_yx_order:
        orow = np.swapaxes(orow, 0, 1)
    row[:] = orow

    col = egrid.get_coords(0, staggerloc=ESMF.StaggerLoc.CENTER)
    ocol = ovalue_stacked[1, ...]
    if is_yx_order:
        ocol = np.swapaxes(ocol, 0, 1)
    col[:] = ocol

    ####################################################################################################################

    # Use a logical or operation to merge with value_mask if present
    if value_mask is not None:
        # convert to boolean to make sure
        value_mask = value_mask.astype(bool)
        # do the logical or operation selecting values
        value_mask = np.logical_or(value_mask, ogrid.get_mask(create=True))
    else:
        value_mask = ogrid.get_mask()
    # Follows SCRIP convention where 1 is unmasked and 0 is masked.
    if value_mask is not None:
        esmf_mask = np.invert(value_mask).astype(np.int32)
        esmf_mask = np.swapaxes(esmf_mask, 0, 1)
        egrid.add_item(ESMF.GridItem.MASK, staggerloc=ESMF.StaggerLoc.CENTER, from_file=False)
        egrid.mask[0][:] = esmf_mask

    # Attempt to access corners if requested.
    if regrid_method == 'auto' and ogrid.has_bounds:
        regrid_method = RegridMethod.CONSERVE

    if regrid_method == RegridMethod.CONSERVE:
        # Conversion to ESMF objects requires an expanded grid (non-vectorized).
        # TODO: Create ESMF grids from vectorized OCGIS grids.
        expand_grid(ogrid)
        # Convert to ESMF corners from OCGIS corners.
        corners_esmf = np.zeros([2] + [element + 1 for element in max_index], dtype=col.dtype)
        get_esmf_corners_from_ocgis_corners(ogrid.y.bounds.get_value(), fill=corners_esmf[0, :, :])
        get_esmf_corners_from_ocgis_corners(ogrid.x.bounds.get_value(), fill=corners_esmf[1, :, :])

        # adding corners. first tell the grid object to allocate corners
        egrid.add_coords(staggerloc=[ESMF.StaggerLoc.CORNER])
        # get the coordinate pointers and set the coordinates
        grid_corner = egrid.coords[ESMF.StaggerLoc.CORNER]
        # Note indexing is reversed for ESMF v. OCGIS. In ESMF, the x-coordinate (longitude) is the first
        # coordinate. If this is periodic, the last column corner wraps/connect to the first. This coordinate must
        # be removed.
        if pkwds['num_peri_dims'] is not None:
            grid_corner[0][:] = corners_esmf[1][0:-1, :]
            grid_corner[1][:] = corners_esmf[0][0:-1, :]
        else:
            grid_corner[0][:] = corners_esmf[1]
            grid_corner[1][:] = corners_esmf[0]

    return egrid


def get_periodicity_parameters(grid):
    """
    Get characteristics of a grid's periodicity. This is only applicable for grids with a spherical coordinate system.
    There are two classifications:
     1. A grid is periodic (i.e. it has global coverage). Periodicity is determined only with the x/longitude dimension.
     2. A grid is non-periodic (i.e. it has regional coverage).

    :param grid: :class:`~ocgis.Grid`
    :return: A dictionary containing periodicity parameters.
    :rtype: dict
    """
    # Check if grid may be flagged as "periodic" by determining if its extent is global. Use the centroids and the grid
    # resolution to determine this.
    is_periodic = False
    col = grid.x.get_value()
    resolution = grid.resolution_x
    min_col, max_col = col.min(), col.max()
    # Work only with unwrapped coordinates.
    if min_col < 0:
        select = col < 0
        if select.any():
            max_col = np.max(col[col < 0]) + 360.
        select = col >= 0
        if select.any():
            min_col = np.min(col[col >= 0])
    # Check the min and max column values are within a tolerance (the grid resolution) of global (0 to 360) edges.
    if (0. - resolution) <= min_col <= (0. + resolution):
        min_periodic = True
    else:
        min_periodic = False
    if (360. - resolution) <= max_col <= (360. + resolution):
        max_periodic = True
    else:
        max_periodic = False
    if min_periodic and max_periodic:
        is_periodic = True

    # If the grid is periodic, set the appropriate parameters.
    if is_periodic:
        num_peri_dims = 1
        periodic_dim = 0
        pole_dim = 1
    else:
        num_peri_dims, pole_dim, periodic_dim = [None] * 3

    ret = {'num_peri_dims': num_peri_dims, 'pole_dim': pole_dim, 'periodic_dim': periodic_dim}

    return ret


def iter_esmf_fields(ofield, regrid_method='auto', value_mask=None, split=True):
    """
    For all data or a single time coordinate, yield an ESMF :class:`~ESMF.driver.field.Field` from the input OCGIS field
    ``ofield``. Only one realization and level are allowed.

    :param ofield: The input OCGIS Field object.
    :type ofield: :class:`ocgis.Field`
    :param regrid_method: See :func:`~ocgis.regrid.base.get_esmf_grid`.
    :param value_mask: See :func:`~ocgis.regrid.base.get_esmf_grid`.
    :param bool split: If ``True``, yield a single time slice/coordinate from the source field. If ``False``, yield all
     time coordinates. When ``False``, OCGIS uses ESMF's ``ndbounds`` argument to field creation. Use ``True`` if
     there are memory limitations. Use ``False`` for faster performance.
    :rtype: tuple(str, :class:`ESMF.driver.field.Field`, int)

    The returned tuple elements are:

    ===== ================================= ===================================================================================================
    Index Type                              Description
    ===== ================================= ===================================================================================================
    0     str                               The variable name currently being converted.
    1     :class:`~ESMF.driver.field.Field` The ESMF Field object.
    2     int                               The current time index of the yielded ESMF field. If ``split=False``, This will be ``slice(None)``.
    ===== ================================= ===================================================================================================

    :raises: AssertionError
    """
    # Only one level and realization allowed.
    if ofield.level is not None and ofield.level.ndim > 0:
        assert ofield.level.shape[0] == 1
    if ofield.realization is not None:
        assert ofield.realization.shape[0] == 1

    # Retrieve the mask from the first variable.
    if value_mask is None:
        if ofield.time is not None:
            sfield = ofield.get_field_slice({'time': 0})
        else:
            sfield = ofield
        archetype = sfield.data_variables[0]
        value_mask = archetype.get_mask()
        if value_mask is not None and value_mask.ndim == 3 and value_mask.shape[0] == 1:
            value_mask = np.squeeze(value_mask, 0)

    # Create the ESMF grid.
    egrid = get_esmf_grid(ofield.grid, regrid_method=regrid_method, value_mask=value_mask)

    # Produce a new ESMF field for each variable.
    if ofield.time is not None:
        time_name = ofield.time.dimensions[0].name
    else:
        # If there is no time, there is no need to split anything by the time slice.
        split = False
        time_name = None

    # These dimensions will become singletons.
    dimension_names_to_squeeze = []
    if ofield.level is not None and ofield.level.ndim > 0:
        dimension_names_to_squeeze.append(ofield.level.dimensions[0].name)
    if ofield.realization is not None:
        dimension_names_to_squeeze.append(ofield.realization.dimensions[0].name)

    for variable in ofield.data_variables:
        variable = variable.extract()
        dimensions_to_squeeze = [idx for idx, d in enumerate(variable.dimensions) if
                                 d.name in dimension_names_to_squeeze]
        extra_dimensions = [d.name for d in variable.dimensions if d.name != time_name]
        slice_template = {name: None for name in extra_dimensions}
        variable_name = variable.name
        if split:
            for tidx in range(ofield.time.shape[0]):
                efield = ESMF.Field(egrid, name=variable_name)
                slice_template[time_name] = tidx
                fill_data = np.squeeze(variable[slice_template].get_value(), axis=dimensions_to_squeeze)
                fill_data = np.swapaxes(fill_data, 0, 1)
                efield.data[:] = fill_data
                yield variable_name, efield, tidx
        else:
            if ofield.time is not None:
                ndbounds = [ofield.time.shape[0]]
                slice_template[time_name] = None
            else:
                ndbounds = None
            efield = ESMF.Field(egrid, name=variable_name, ndbounds=ndbounds)
            slice_template[ofield.x.dimensions[0].name] = None
            slice_template[ofield.y.dimensions[0].name] = None
            fill_data = np.squeeze(variable[slice_template].get_value(), axis=dimensions_to_squeeze)
            fill_data = np.swapaxes(fill_data, 0, -1)
            efield.data[:] = fill_data
            tidx = slice(None)
            yield variable_name, efield, tidx


def check_fields_for_regridding(source, destination, regrid_method='auto'):
    """
    Perform a standard series of checks on regridding inputs.

    :param source: The source field.
    :type source: :class:`ocgis.Field`
    :param destination: The destination field.
    :type destination: :class:`ocgis.Field`
    :param regrid_method: If ``'auto'``, attempt to do conservative regridding if bounds/corners are available on both
      fields. Otherwise, do bilinear regridding. This may also be an ESMF regrid method flag.
    :type regrid_method: str or :attr:`ESMF.api.constants.RegridMethod`
    :raises: RegriddingError, CornersInconsistentError
    """

    # Check field objects are used.
    for element in [source, destination]:
        if not isinstance(element, Field):
            raise RegriddingError('OCGIS field objects only for regridding.')

    # Check their are grids on source and destination. Only structured grids are supported at this time.
    for element in [source, destination]:
        if element.grid is None:
            raise RegriddingError('Fields must have grids to do regridding.')

    # Check coordinate systems #########################################################################################

    for element in [source, destination]:
        if not isinstance(element.crs, Spherical):
            msg_a = 'Only spherical coordinate systems allowed for regridding.'
            raise RegriddingError(msg_a)

    if source.crs != destination.crs:
        msg = 'Source and destination coordinate systems must be equal.'
        raise RegriddingError(msg)

    # Check corners are available on all inputs ########################################################################

    if regrid_method == ESMF.RegridMethod.CONSERVE:
        has_corners_source = source.grid.has_bounds
        has_corners_destination = destination.grid.has_bounds
        if not has_corners_source or not has_corners_destination:
            msg = 'Corners are not available on all sources and destination. Consider changing "regrid_method".'
            raise CornersInconsistentError(msg)


def regrid_field(source, destination, regrid_method='auto', value_mask=None, split=True):
    """
    Regrid ``source`` data to match the grid of ``destination``.

    :param source: The source field.
    :type source: :class:`ocgis.Field`
    :param destination: The destination field.
    :type destination: :class:`ocgis.Field`
    :param regrid_method: See :func:`~ocgis.regrid.base.get_esmf_grid`.
    :param value_mask: See :func:`~ocgis.regrid.base.iter_esmf_fields`.
    :type value_mask: :class:`numpy.ndarray`
    :param bool split: See :func:`~ocgis.regrid.base.iter_esmf_fields`.
    :rtype: :class:`ocgis.Field`
    """

    # This function runs a series of asserts to make sure the sources and destination are compatible.
    check_fields_for_regridding(source, destination, regrid_method=regrid_method)

    # Regrid each source.
    ocgis_lh(logger='iter_regridded_fields', msg='starting source regrid loop', level=logging.DEBUG)
    # for source in sources:
    build = True
    fills = {}
    for variable_name, src_efield, tidx in iter_esmf_fields(source, regrid_method=regrid_method,
                                                            value_mask=value_mask, split=split):
        # We need to generate new variables given the change in shape
        if variable_name not in fills:
            if source.time is not None:
                new_dimensions = list(source.time.dimensions) + list(destination.grid.dimensions)
            else:
                new_dimensions = list(destination.grid.dimensions)
            source_variable = source[variable_name]
            new_variable = Variable(name=variable_name, dimensions=new_dimensions, dtype=source_variable.dtype,
                                    fill_value=source_variable.fill_value)
            fills[variable_name] = new_variable

        # Only build the regrid objects once.
        if build:
            # Build the destination grid once.
            esmf_destination_grid = get_esmf_grid(destination.grid, regrid_method=regrid_method, value_mask=value_mask)

            # Check for corners on the destination grid. If they exist, conservative regridding is possible.
            if regrid_method == 'auto':
                if get_esmf_grid_has_corners(esmf_destination_grid) and get_esmf_grid_has_corners(src_efield.grid):
                    regrid_method = ESMF.RegridMethod.CONSERVE
                else:
                    regrid_method = None

            if split:
                ndbounds = None
            else:
                ndbounds = [source.time.shape[0]]

            # Prepare the regridded sourced field. This amounts to exchanging the grids between the objects.
            regridded_source = source.copy()
            regridded_source.grid.extract(clean_break=True)
            regridded_source.set_grid(destination.grid.extract())

            build = False

        dst_efield = ESMF.Field(esmf_destination_grid, name='destination', ndbounds=ndbounds)
        fill_variable = fills[variable_name]
        fv = fill_variable.fill_value
        if fv is None:
            fv = np.ma.array([0], dtype=fill_variable.dtype).fill_value
        dst_efield.data.fill(fv)
        # Construct the regrid object. Weight generation actually occurs in this call.
        regrid = ESMF.Regrid(src_efield, dst_efield, unmapped_action=ESMF.UnmappedAction.IGNORE,
                             regrid_method=regrid_method, src_mask_values=[0], dst_mask_values=[0])
        # Perform the regrid operation. "zero_region" only fills values involved with regridding.
        regridded_esmf_field = regrid(src_efield, dst_efield, zero_region=ESMF.Region.SELECT)
        e_data = regridded_esmf_field.data
        unmapped_mask = e_data[:] == fv
        # If all data is masked, raise an exception.
        if unmapped_mask.all():
            # Destroy ESMF objects.
            destroy_esmf_objects([regrid, dst_efield, esmf_destination_grid])
            msg = 'All regridded elements are masked. Do the input spatial extents overlap?'
            raise RegriddingError(msg)
        # Fill the new variables.
        fv_value = fill_variable.get_value()
        fv_mask = fill_variable.get_mask(create=True)

        has_split_tidx = fv_value.ndim == 3
        if has_split_tidx:
            fill_data = np.swapaxes(e_data, 0, -1)
            fv_value[tidx, :, :] = fill_data
        else:
            # Assume no time index.
            fill_data = np.swapaxes(e_data, 0, 1)
            fv_value[:, :] = fill_data

        if regridded_esmf_field.grid.mask[0] is not None:
            e_mask = np.invert(regridded_esmf_field.grid.mask[0].astype(bool))
            if has_split_tidx:
                fill_data = np.swapaxes(e_mask, 0, -1)
                fv_mask[tidx, :, :] = fill_data
            else:
                # Assume no time index.
                fill_data = np.swapaxes(e_mask, 0, 1)
                fv_mask[:, :] = fill_data

        if has_split_tidx:
            unmapped_mask = np.swapaxes(unmapped_mask, 0, -1)
            fill_variable_mask = np.logical_or(unmapped_mask, fv_mask[tidx, :, :])
        else:
            # Assume no time index.
            unmapped_mask = np.swapaxes(unmapped_mask, 0, 1)
            fill_variable_mask = np.logical_or(unmapped_mask, fv_mask[:, :])

        if has_split_tidx:
            fv_mask[tidx, :, :] = fill_variable_mask
        else:
            # Assume no time index.
            fv_mask[:, :] = fill_variable_mask
        fill_variable.set_mask(fv_mask)

        # Destroy ESMF objects, but keep the grid until all splits have finished. If split=False, there is only one
        # split.
        destroy_esmf_objects([regrid, dst_efield, src_efield])

        # Create a new variable collection and add the variables to the output field.
        # source.variables = VariableCollection()
        for v in list(fills.values()):
            regridded_source.add_variable(v, is_data=True, force=True)

    destroy_esmf_objects([esmf_destination_grid])

    return regridded_source


def destroy_esmf_objects(objs):
    for obj in objs:
        obj.destroy()
