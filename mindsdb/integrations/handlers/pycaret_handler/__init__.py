from mindsdb.integrations.libs.const import HANDLER_TYPE
from mindsdb.utilities import log
from .__about__ import __version__ as version, __description__ as description

logger = log.getLogger(__name__)

try:
    from .pycaret_handler import PyCaretHandler as Handler
    import_error = None
    logger.info("PyCaret successfully imported")
except Exception as e:
    Handler = None
    import_error = e
    logger.info("Error Importing PyCaret")

title = 'PyCaret'
name = 'pycaret'
type = HANDLER_TYPE.ML
permanent = True

__all__ = [
    'Handler', 'version', 'name', 'type', 'title', 'description', 'import_error'
]
