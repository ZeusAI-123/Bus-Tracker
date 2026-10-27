
CREATE TABLE gtfs_routes (route_id text PRIMARY KEY, route_short_name text, route_long_name text, route_type int);
CREATE TABLE gtfs_stops (stop_id text PRIMARY KEY, stop_name text, stop_lat double precision, stop_lon double precision, wheelchair_boarding int);
CREATE TABLE gtfs_trips (route_id text, service_id text, trip_id text PRIMARY KEY, direction_id int, shape_id text, block_id text, wheelchair_accessible int);
CREATE TABLE gtfs_stop_times (trip_id text, stop_id text, stop_sequence int, arrival_time text, departure_time text, shape_dist_traveled double precision);
CREATE TABLE gtfs_shapes (shape_id text, shape_pt_lat double precision, shape_pt_lon double precision, shape_pt_sequence int, shape_dist_traveled double precision);
CREATE TABLE buses (bus_id text PRIMARY KEY, route_id text, capacity int, wheelchair_accessible boolean, home_lat double precision, home_lon double precision);
CREATE TABLE driver_shifts (service_date date, driver_id text, bus_id text, shift_start text, shift_end text, status text);
CREATE TABLE maintenance_reports (service_date date, bus_id text, reported_at text, issue text, severity text);
CREATE TABLE road_closures (service_date date, route_id text, from_stop text, to_stop text, reason text, "start" text, "end" text);
CREATE TABLE events (service_date date, stop_id text, name text, "start" text, "end" text, expected_extra_pax int);
CREATE TABLE train_delays (service_date date, station_stop_id text, train text, scheduled_arr text, actual_arr text, transfer_pax int, connecting_route text);
CREATE TABLE passenger_counts (service_date date, stop_id text, window_start text, waiting int, wheelchair_waiting int);
CREATE TABLE weather_hourly (service_date date, hour int, precip_mm double precision, temp_c double precision, source text);

COPY gtfs_routes FROM '/data/sample/gtfs_routes.csv' CSV HEADER;
COPY gtfs_stops FROM '/data/sample/gtfs_stops.csv' CSV HEADER;
COPY gtfs_trips FROM '/data/sample/gtfs_trips.csv' CSV HEADER;
COPY gtfs_stop_times FROM '/data/sample/gtfs_stop_times.csv' CSV HEADER;
COPY gtfs_shapes FROM '/data/sample/gtfs_shapes.csv' CSV HEADER;
COPY buses FROM '/data/sample/buses.csv' CSV HEADER;
COPY driver_shifts FROM '/data/sample/driver_shifts.csv' CSV HEADER;
COPY maintenance_reports FROM '/data/sample/maintenance_reports.csv' CSV HEADER;
COPY road_closures FROM '/data/sample/road_closures.csv' CSV HEADER;
COPY events FROM '/data/sample/events.csv' CSV HEADER;
COPY train_delays FROM '/data/sample/train_delays.csv' CSV HEADER;
COPY passenger_counts FROM '/data/sample/passenger_counts.csv' CSV HEADER;
COPY weather_hourly FROM '/data/sample/weather_hourly.csv' CSV HEADER;
