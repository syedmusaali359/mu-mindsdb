import time
import importlib
import traceback
import datetime as dt
from collections import UserDict

from mindsdb_sql import parse_sql
from mindsdb_sql.parser.ast import Identifier, Select, Star, NativeQuery

import mindsdb.interfaces.storage.db as db
from mindsdb.api.mysql.mysql_proxy.classes.sql_query import SQLQuery
from mindsdb.integrations.utilities.sql_utils import make_sql_session
from mindsdb.integrations.libs.const import PREDICTOR_STATUS
from mindsdb.interfaces.storage.model_fs import ModelStorage, HandlerStorage
from mindsdb.interfaces.model.functions import get_model_records
from mindsdb.integrations.utilities.utils import format_exception_error
import mindsdb.utilities.profiler as profiler
from mindsdb.utilities.functions import mark_process
from mindsdb.utilities.context import context as ctx
from mindsdb.utilities.config import Config
from mindsdb.utilities import log

logger = log.getLogger(__name__)


class HandlersCache(UserDict):
    def __init__(self, max_size: int = 5) -> None:
        self._max_size = max_size
        super().__init__()

    def __setitem__(self, key, value) -> None:
        if len(self.data) > self._max_size:
            sorted_elements = sorted(
                self.data.items(),
                key=lambda x: x[1]['last_usage_at']
            )
            del self.data[sorted_elements[0][0]]
        self.data[key] = {
            'last_usage_at': time.time(),
            'handler': value
        }

    def __getitem__(self, key: int) -> object:
        el = super().__getitem__(key)
        el['last_usage_at'] = time.time()
        return el['handler']


handlers_cacher = HandlersCache()


@mark_process(name='learn')
def predict_process(payload, dataframe):
    db.init()
    integration_id = payload['handler_meta']['integration_id']
    predictor_record = payload['predictor_record']
    args = payload['args']
    module_path = payload['handler_meta']['module_path']
    class_name = payload['handler_meta']['class_name']
    ml_engine_name = payload['handler_meta']['engine']

    module = importlib.import_module(module_path)
    HandlerClass = getattr(module, class_name)
    handler_class = HandlerClass

    if predictor_record.id not in handlers_cacher:
        handlerStorage = HandlerStorage(integration_id)
        modelStorage = ModelStorage(predictor_record.id)
        ml_handler = handler_class(
            engine_storage=handlerStorage,
            model_storage=modelStorage,
        )
        handlers_cacher[predictor_record.id] = ml_handler
    else:
        ml_handler = handlers_cacher[predictor_record.id]

    if ml_engine_name == 'lightwood':
        args['code'] = predictor_record.code
        args['target'] = predictor_record.to_predict[0]
        args['dtype_dict'] = predictor_record.dtype_dict
        args['learn_args'] = predictor_record.learn_args

    if ml_engine_name == 'langchain':
        from mindsdb.api.mysql.mysql_proxy.controllers import SessionController
        from mindsdb.api.mysql.mysql_proxy.executor.executor_commands import ExecuteCommands

        sql_session = SessionController()
        sql_session.database = 'mindsdb'

        command_executor = ExecuteCommands(sql_session, executor=None)

        args['executor'] = command_executor

    predictions = ml_handler.predict(dataframe, args)
    ml_handler.close()
    return predictions


@mark_process(name='learn')
def learn_process(payload, dataframe):
    ctx.profiling = {
        'level': 0,
        'enabled': True,
        'pointer': None,
        'tree': None
    }
    profiler.set_meta(query='learn_process', api='http', environment=Config().get('environment'))
    with profiler.Context('learn_process'):
        from mindsdb.interfaces.database.database import DatabaseController
        db.init()

        data_integration_ref = payload['data_integration_ref']
        problem_definition = payload['problem_definition']
        fetch_data_query = payload['fetch_data_query']
        project_name = payload['project_name']
        predictor_id = payload['model_id']
        # engine = payload['handler_meta']['engine']
        integration_id = payload['handler_meta']['integration_id']
        base_predictor_id = payload.get('base_model_id')
        set_active = payload['set_active']
        class_path = (payload['handler_meta']['module_path'], payload['handler_meta']['class_name'])

        try:
            target = problem_definition.get('target', None)
            training_data_df = None
            if data_integration_ref is not None:
                database_controller = DatabaseController()
                sql_session = make_sql_session()
                if data_integration_ref['type'] == 'integration':
                    integration_name = database_controller.get_integration(data_integration_ref['id'])['name']
                    query = Select(
                        targets=[Star()],
                        from_table=NativeQuery(
                            integration=Identifier(integration_name),
                            query=fetch_data_query
                        )
                    )
                    sqlquery = SQLQuery(query, session=sql_session)
                elif data_integration_ref['type'] == 'view':
                    project = database_controller.get_project(project_name)
                    query_ast = parse_sql(fetch_data_query, dialect='mindsdb')
                    view_meta = project.query_view(query_ast)
                    sqlquery = SQLQuery(view_meta['query_ast'], session=sql_session)

                result = sqlquery.fetch(view='dataframe')
                training_data_df = result['result']

            training_data_columns_count, training_data_rows_count = 0, 0
            if training_data_df is not None:
                training_data_columns_count = len(training_data_df.columns)
                training_data_rows_count = len(training_data_df)

            predictor_record = db.Predictor.query.with_for_update().get(predictor_id)
            predictor_record.training_data_columns_count = training_data_columns_count
            predictor_record.training_data_rows_count = training_data_rows_count
            db.session.commit()

            module_name, class_name = class_path
            module = importlib.import_module(module_name)
            HandlerClass = getattr(module, class_name)

            handlerStorage = HandlerStorage(integration_id)
            modelStorage = ModelStorage(predictor_id)
            modelStorage.fileStorage.push()     # FIXME

            kwargs = {}
            if base_predictor_id is not None:
                kwargs['base_model_storage'] = ModelStorage(base_predictor_id)
                kwargs['base_model_storage'].fileStorage.pull()

            ml_handler = HandlerClass(
                engine_storage=handlerStorage,
                model_storage=modelStorage,
                **kwargs
            )
            handlers_cacher[predictor_record.id] = ml_handler

            if not ml_handler.generative:
                if training_data_df is not None and target not in training_data_df.columns:
                    raise Exception(
                        f'Prediction target "{target}" not found in training dataframe: {list(training_data_df.columns)}')

            # create new model
            if base_predictor_id is None:
                with profiler.Context('create'):
                    ml_handler.create(target, df=training_data_df, args=problem_definition)

            # fine-tune (partially train) existing model
            else:
                # load model from previous version, use it as starting point
                with profiler.Context('finetune'):
                    problem_definition['base_model_id'] = base_predictor_id
                    ml_handler.finetune(df=training_data_df, args=problem_definition)

            predictor_record.status = PREDICTOR_STATUS.COMPLETE
            predictor_record.active = set_active
            db.session.commit()
            # if retrain and set_active after success creation
            if set_active is True:
                models = get_model_records(
                    name=predictor_record.name,
                    project_id=predictor_record.project_id,
                    active=None
                )
                for model in models:
                    model.active = False
                models = [x for x in models if x.status == PREDICTOR_STATUS.COMPLETE]
                models.sort(key=lambda x: x.created_at)
                models[-1].active = True
        except Exception as e:
            logger.error(traceback.format_exc())
            error_message = format_exception_error(e)

            predictor_record = db.Predictor.query.with_for_update().get(predictor_id)
            predictor_record.data = {"error": error_message}
            predictor_record.status = PREDICTOR_STATUS.ERROR
            db.session.commit()

        predictor_record.training_stop_at = dt.datetime.now()
        db.session.commit()
