# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2017-2019 CERN.
# Copyright (C)      2022 TU Wien.
#
# Invenio is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Aggregation classes."""

from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
from functools import wraps

from dateutil import parser
from dateutil.relativedelta import relativedelta
from invenio_search import current_search_client
from invenio_search.engine import dsl, search
from invenio_search.utils import prefix_index

from .utils import get_bucket_size

SUPPORTED_INTERVALS = OrderedDict(
    [
        ("hour", "%Y-%m-%dT%H"),
        ("day", "%Y-%m-%d"),
        ("month", "%Y-%m"),
    ]
)

INTERVAL_DELTAS = {
    "hour": lambda batch_size: relativedelta(hours=batch_size),
    "day": lambda batch_size: relativedelta(days=batch_size),
    "month": lambda batch_size: relativedelta(months=batch_size),
}


def filter_robots(query):
    """Modify a search query so that robot events are filtered out."""
    return query.filter("term", is_robot=False)


def format_range_dt(dt, interval):
    """Format range filter datetime to the closest aggregation interval."""
    dt_rounding_map = {"hour": "h", "day": "d", "month": "M", "year": "y"}

    if not isinstance(dt, str):
        dt = dt.replace(microsecond=0).isoformat()
    return "{0}||/{1}".format(dt, dt_rounding_map[interval])


class BookmarkAPI(object):
    """Bookmark API class.

    It provides an interface that lets us interact with a bookmark.
    """

    MAPPINGS = {
        "mappings": {
            "date_detection": False,
            "properties": {
                "date": {"type": "date", "format": "date_optional_time"},
                "aggregation_type": {"type": "keyword"},
            },
        }
    }

    def __init__(self, client, agg_type, agg_interval):
        """Construct bookmark instance.

        :param client: search client
        :param agg_type: aggregation type for the bookmark
        """
        self.bookmark_index = prefix_index("stats-bookmarks")
        self.client = client
        self.agg_type = agg_type
        self.agg_interval = agg_interval

    def _ensure_index_exists(func):
        """Decorator for ensuring the bookmarks index exists."""

        @wraps(func)
        def wrapped(self, *args, **kwargs):
            if not dsl.Index(self.bookmark_index, using=self.client).exists():
                self.client.indices.create(
                    index=self.bookmark_index, body=BookmarkAPI.MAPPINGS
                )
            return func(self, *args, **kwargs)

        return wrapped

    @_ensure_index_exists
    def set_bookmark(self, value):
        """Set bookmark for starting next aggregation."""
        self.client.index(
            index=self.bookmark_index,
            body={"date": value, "aggregation_type": self.agg_type},
        )

    @_ensure_index_exists
    def get_bookmark(self):
        """Get last aggregation date."""
        # retrieve the oldest bookmark
        query_bookmark = (
            dsl.Search(using=self.client, index=self.bookmark_index)
            .filter("term", aggregation_type=self.agg_type)
            .sort({"date": {"order": "desc"}})[0:1]  # fetch one document only
        )
        bookmark = next(iter(query_bookmark.execute()), None)
        if bookmark:
            return datetime.strptime(
                bookmark.date, SUPPORTED_INTERVALS[self.agg_interval]
            )

    @_ensure_index_exists
    def list_bookmarks(self, start_date=None, end_date=None, limit=None):
        """List bookmarks."""
        query = (
            dsl.Search(
                using=self.client,
                index=self.bookmark_index,
            )
            .filter("term", aggregation_type=self.agg_type)
            .sort({"date": {"order": "desc"}})
        )

        range_args = {}
        if start_date:
            range_args["gte"] = format_range_dt(start_date, self.agg_interval)
        if end_date:
            range_args["lte"] = format_range_dt(end_date)
        if range_args:
            query = query.filter("range", date=range_args)

        return query[0:limit].execute() if limit else query.scan()


ALLOWED_METRICS = {
    "avg",
    "cardinality",
    "extended_stats",
    "geo_centroid",
    "max",
    "min",
    "percentiles",
    "stats",
    "sum",
}


class StatAggregator(object):
    """Generic aggregation class.

    This aggregation class queries search events and creates a new
    search document for each aggregated day/month/year... This enables
    to "compress" the events and keep only relevant information.

    The expected events shoud have at least those two fields:

    .. code-block:: json

        {
            "timestamp": "<ISO DATE TIME>",
            "field_on_which_we_aggregate": "<A VALUE>"
        }

    The resulting aggregation documents will be of the form:

    .. code-block:: json

        {
            "timestamp": "<ISO DATE TIME>",
            "field_on_which_we_aggregate": "<A VALUE>",
            "count": "<NUMBER OF OCCURENCE OF THIS EVENT>",
            "field_metric": "<METRIC CALCULATION ON A FIELD>"
        }

    This aggregator saves a bookmark document after each run. This bookmark
    is used to aggregate new events without having to redo the old ones.
    """

    def __init__(
        self,
        name,
        event,
        client=None,
        field=None,
        metric_fields=None,
        copy_fields=None,
        query_modifiers=None,
        interval="day",
        index_interval="month",
        batch_size=7,
    ):
        """Construct aggregator instance.

        :param event: aggregated event.
        :param client: search client.
        :param field: field on which the aggregation will be done.
        :param metric_fields: dictionary of fields on which a
            metric aggregation will be computed. The format of the dictionary
            is "destination field" ->
            tuple("metric type", "source field", "metric_options").
        :param copy_fields: list of fields which are copied from the raw events
            into the aggregation.
        :param query_modifiers: list of functions modifying the raw events
            query. By default the query_modifiers are [filter_robots].
        :param interval: aggregation time window. default: month.
        :param index_interval: time window of the search indices which
            will contain the resulting aggregations.
        :param batch_size: max number of hours/days/months for which raw events
            are being fetched in one query. This number has to be coherent with
            the interval.
        """
        self.name = name
        self.event = event
        self.event_index = prefix_index("events-stats-{}".format(event))
        self.client = client or current_search_client
        self.index = prefix_index("stats-{}".format(event))
        self.field = field
        self.metric_fields = metric_fields or {}
        self.interval = interval
        self.batch_size = batch_size
        self.doc_id_suffix = SUPPORTED_INTERVALS[interval]
        self.index_interval = index_interval
        self.index_name_suffix = SUPPORTED_INTERVALS[index_interval]
        self.indices = set()
        self.copy_fields = copy_fields or {}
        self.query_modifiers = (
            query_modifiers if query_modifiers is not None else [filter_robots]
        )
        self.bookmark_api = BookmarkAPI(self.client, self.name, self.interval)

        if any(v not in ALLOWED_METRICS for k, (v, _, _) in self.metric_fields.items()):
            raise (
                ValueError(
                    "Metric aggregation type should be one of [{}]".format(
                        ", ".join(ALLOWED_METRICS)
                    )
                )
            )

        if list(SUPPORTED_INTERVALS.keys()).index(interval) > list(
            SUPPORTED_INTERVALS.keys()
        ).index(index_interval):
            raise (
                ValueError("Aggregation interval should be shorter than index interval")
            )

    def _get_oldest_event_timestamp(self):
        """Search for the oldest event timestamp."""
        # Retrieve the oldest event in order to start aggregation
        # from there
        query_events = dsl.Search(using=self.client, index=self.event_index).sort(
            {"timestamp": {"order": "asc"}}
        )[0:1]
        result = query_events.execute()
        # There might not be any events yet if the first event have been
        # indexed but the indices have not been refreshed yet.
        if len(result) == 0:
            return None
        return parser.parse(result[0]["timestamp"])

    def agg_iter(self, lower_limit, upper_limit):
        """Aggregate and return dictionary to be indexed in ES."""
        aggregation_data = {}
        start_date = format_range_dt(lower_limit, self.interval)
        end_date = format_range_dt(upper_limit, self.interval)
        agg_query = dsl.Search(using=self.client, index=self.event_index).filter(
            "range", timestamp={"gte": start_date, "lte": end_date}
        )
        self.agg_query = agg_query

        # apply query modifiers
        for modifier in self.query_modifiers:
            self.agg_query = modifier(self.agg_query)

        # TODO: remove histogram bucket
        histogram = self.agg_query.aggs.bucket(
            "histogram", "date_histogram", field="timestamp", interval=self.interval
        )
        bucket_size = get_bucket_size(
            self.client,
            self.event_index,
            self.field,
            start_date=start_date,
            end_date=end_date,
        )
        if bucket_size > 0:
            terms = histogram.bucket(
                "terms", "terms", field=self.field, size=bucket_size
            )
        else:
            terms = histogram.bucket(
                "terms",
                "terms",
                field=self.field,
            )

        terms.metric("top_hit", "top_hits", size=1, sort={"timestamp": "desc"})
        for dst, (metric, src, opts) in self.metric_fields.items():
            terms.metric(dst, metric, field=src, **opts)

        results = self.agg_query.execute()
        for interval in results.aggregations["histogram"].buckets:
            interval_date = datetime.strptime(
                interval["key_as_string"], "%Y-%m-%dT%H:%M:%S"
            )
            for aggregation in interval["terms"].buckets:
                aggregation_data["timestamp"] = interval_date.isoformat()
                aggregation_data[self.field] = aggregation["key"]
                aggregation_data["count"] = aggregation["doc_count"]

                if self.metric_fields:
                    for f in self.metric_fields:
                        aggregation_data[f] = aggregation[f]["value"]

                doc = aggregation.top_hit.hits.hits[0]["_source"]
                for destination, source in self.copy_fields.items():
                    if isinstance(source, str):
                        aggregation_data[destination] = doc[source]
                    else:
                        aggregation_data[destination] = source(doc, aggregation_data)

                index_name = "stats-{0}-{1}".format(
                    self.event, interval_date.strftime(self.index_name_suffix)
                )
                self.indices.add(index_name)

                yield {
                    "_id": "{0}-{1}".format(
                        aggregation["key"], interval_date.strftime(self.doc_id_suffix)
                    ),
                    "_index": prefix_index(index_name),
                    "_source": aggregation_data,
                }
                self.has_events = True

    def _next_batch(self, dt):
        return dt + INTERVAL_DELTAS[self.interval](self.batch_size)

    def _upper_limit(self, end_date, lower_limit):
        return min(
            end_date or datetime.max,  # ignore if `None`
            datetime.utcnow(),
            self._next_batch(lower_limit),
        )

    def run(self, start_date=None, end_date=None, update_bookmark=True):
        """Calculate statistics aggregations."""
        # If no events have been indexed there is nothing to aggregate
        if not dsl.Index(self.event_index, using=self.client).exists():
            return

        lower_limit = (
            start_date
            or self.bookmark_api.get_bookmark()
            or self._get_oldest_event_timestamp()
        )
        # Stop here if no bookmark could be estimated.
        if lower_limit is None:
            return

        upper_limit = self._upper_limit(end_date, lower_limit)

        while upper_limit <= datetime.utcnow():
            self.has_events = False
            self.indices = set()

            search.helpers.bulk(
                self.client,
                self.agg_iter(lower_limit, upper_limit),
                stats_only=True,
                chunk_size=50,
            )
            # TODO: see if this is needed...
            # Flush all indices which have been modified
            # for index in self.indices:
            #     current_search.flush_and_refresh(index=index)
            if update_bookmark and self.has_events:
                self.bookmark_api.set_bookmark(
                    upper_limit.strftime(self.doc_id_suffix)
                    or datetime.utcnow().strftime(self.doc_id_suffix)
                )

            lower_limit = self._next_batch(lower_limit)
            upper_limit = self._upper_limit(end_date, lower_limit)
            if lower_limit > upper_limit:
                break

    def list_bookmarks(self, start_date=None, end_date=None, limit=None):
        """List the aggregation's bookmarks."""
        return self.bookmark_api.list_bookmarks(start_date, end_date, limit)

    def delete(self, start_date=None, end_date=None):
        """Delete aggregation documents."""
        aggs_query = dsl.Search(
            using=self.client,
            index=self.index,
        ).extra(_source=False)

        range_args = {}
        if start_date:
            range_args["gte"] = format_range_dt(start_date, self.interval)
        if end_date:
            range_args["lte"] = format_range_dt(end_date, self.interval)
        if range_args:
            aggs_query = aggs_query.filter("range", timestamp=range_args)

        bookmarks_query = (
            dsl.Search(
                using=self.client,
                index=self.bookmark_api.bookmark_index,
            )
            .filter("term", aggregation_type=self.name)
            .sort({"date": {"order": "desc"}})
        )

        if range_args:
            bookmarks_query = bookmarks_query.filter("range", date=range_args)

        def _delete_actions():
            for query in (aggs_query, bookmarks_query):
                affected_indices = set()
                for doc in query.scan():
                    affected_indices.add(doc.meta.index)
                    yield {
                        "_index": doc.meta.index,
                        "_op_type": "delete",
                        "_id": doc.meta.id,
                    }
                current_search_client.indices.flush(
                    index=",".join(affected_indices), wait_if_ongoing=True
                )

        search.helpers.bulk(self.client, _delete_actions(), refresh=True)
