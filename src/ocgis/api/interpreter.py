from ocgis import exc, env
from ocgis.api import definition
from ocgis.spatial.union import union_geom_dicts
from ocgis.conv.meta import MetaConverter
from ocgis.conv.converter import OcgConverter
from subset import SubsetOperation

## TODO: add method to estimate request size

class Interpreter(object):
    '''Superclass for custom interpreter frameworks.
    
    ops :: OcgOperations'''
    
    def __init__(self,ops):
        self.ops = ops
        
    @classmethod
    def get_interpreter(cls,ops):
        '''Select interpreter class.'''
        
        imap = {'ocg':OcgInterpreter}
        try:
            return(imap[ops.backend](ops))
        except KeyError:
            raise(exc.InterpreterNotRecognized)
        
    def check(self):
        '''Validate operation definition dictionary.'''
        raise(NotImplementedError)
    
    def execute(self):
        '''Run requested operations and return a path to the output file or a
        NumPy-based output object depending on specification.'''
        raise(NotImplementedError)


class OcgInterpreter(Interpreter):
    '''The OCGIS interpreter and execution framework.'''
    
    def check(self):
        ## perform basic validation checks
#        definition.validate_update_definition(self.ops)
        ## by parsing operational commands, determine iterator mode for
        ## converters
        definition.identify_iterator_mode(self.ops)
    
    def execute(self):
        self.check() ## run validation
        
        ## check for a user-supplied output prefix
        prefix = self.ops.prefix
        if prefix is not None:
            env.BASE_NAME = prefix
        
        if self.ops.select_ugid is not None:
            geom = self.ops._get_object_('geom')
            geom._filter_by_ugid_(self.ops.select_ugid['ugid'])
            
        ## in the case of netcdf output, geometries must be unioned. this is
        ## also true for the case of the selection geometry being requested as
        ## aggregated.
        if self.ops.output_format == 'nc' or self.ops.agg_selection is True:
            self.ops.geom = union_geom_dicts(self.ops.geom)
            
        ## limited operations available for netcdf
        if self.ops.output_format == 'nc':
            self.ops.spatial_operation = 'intersects'
            self.ops.aggregate = False
            self.ops.calc_raw = False
            self.ops.mode = 'raw'
            self.ops.vector_wrap = False
        
        ## if the requested output format is "meta" then no operations are run
        ## and only the operations dictionary is required to generate output.
        if self.ops.output_format == 'meta':
            ## attempt to pull the request url
            request_url = self.ops.request_url
            ret = MetaConverter(self.ops,request_url=request_url).write()
        ## this is the standard request for other output types.
        else:
            ## the operations object performs subsetting and calculations
            so = SubsetOperation(self.ops,serial=env.SERIAL,nprocs=7)
            ## if there is no grouping on the output files, a singe converter is
            ## is needed
            if self.ops.output_grouping is None:
                Conv = OcgConverter.get_converter(self.ops.output_format)
                conv = Conv(so,wd=env.WORKSPACE,base_name=env.BASE_NAME,
                            mode=self.ops.mode)
                ret = conv.write()
            else:
                raise(NotImplementedError)
        return(ret)
