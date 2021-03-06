import dateutil.parser
import logging as l
import json
import math

from collections import defaultdict

from shapely import affinity
from shapely.geometry import Point, LineString

from asfault import config as c
from asfault.network import *


def get_start_goal_coords(network, start, goal):
    boundary = network.bounds.exterior
    points = (boundary.intersection(start.get_spine()),
              boundary.intersection(goal.get_spine()))
    if points[0].type == 'MultiPoint':
        points = (points[0][0], points[1])
    if points[1].type == 'MultiPoint':
        points = (points[0], points[1][0])

    return points


def random_start_goal(rng, network):
    options = network.get_start_goal_candidates()
    if not options:
        return None, None

    choice = rng.sample(options, 1)[0]
    assert network.is_reachable(*choice)
    return get_start_goal_coords(network, choice[0], choice[1])


def get_nodes(network, pointa, pointb):
    beg_node = network.get_nodes_at(pointa)
    end_node = network.get_nodes_at(pointb)
    if not beg_node or not end_node:
        return None, None

    beg_node = beg_node.pop()
    end_node = end_node.pop()

    return beg_node, end_node


def get_spine_intersection(from_seg, to_seg):
    from_spine = from_seg.get_spine()
    to_spine = to_seg.get_spine()
    intersection = from_spine.intersection(to_spine)

    if intersection.geom_type != 'Point':
        l.debug('Got non-point intersection for %s x %s: %s', str(from_seg),
                str(to_seg), str(intersection))
        intersection = None
        return None

    if intersection.geom_type in (
            'GeometryCollection', 'MultiLineString', 'MultiPoint'):
        if intersection.geoms:
            l.debug('Got intersection type: %s', intersection.geom_type)
            intersection = intersection.geoms[0]
        else:
            intersection = None

    if intersection.geom_type == 'LineString':
        l.debug('Got intersection type: %s', intersection.geom_type)
        intersection = Point(*intersection.coords[0])

    return intersection


def get_path_seg_direction_point(seg, beg_coord, end_coord):
    straight = LineString([beg_coord, end_coord])
    point_a = straight.interpolate(0.25, normalized=True)
    point_b = straight.interpolate(0.75, normalized=True)
    spine = seg.get_spine()
    if not point_a or point_a.geom_type != 'Point':
        l.error('Point is: %s', point_a.geom_type)
        raise ValueError('a is not a point!')
    if not point_b or point_b.geom_type != 'Point':
        l.error('Point is: %s', point_b.geom_type)
        raise ValueError('b is not a point!')
    a_dist = spine.project(point_a, normalized=True)
    b_dist = spine.project(point_b, normalized=True)

    return b_dist > a_dist


def get_path_seg_direction(network, beg_coord, from_seg, to_seg):
    end_coord = None
    if network.is_intersecting_pair(from_seg, to_seg):
        end_coord = get_spine_intersection(from_seg, to_seg)
    elif not end_coord:
        end_coord = to_seg.get_spine().interpolate(0.0, normalized=True)

    return get_path_seg_direction_point(from_seg, beg_coord, end_coord)


def get_path_direction_list(network, start, goal, path):
    assert len(path) > 1
    ret = []

    direction = get_path_seg_direction(network, start, path[0], path[1])
    ret.append(direction)

    if len(path) > 2:
        reachability = network.reachability
        for from_seg, to_seg in zip(path[1:-1], path[2:]):
            assert reachability.edges[from_seg, to_seg]
            if network.is_intersecting_pair(from_seg, to_seg):
                ret.append(ret[-1])
            else:
                direction = reachability.edges[from_seg, to_seg]['direction']
                ret.append(direction)

    from_seg = path[-1]
    beg_coord = path[-2].get_spine().interpolate(1.0, normalized=True)
    direction = get_path_seg_direction_point(from_seg, beg_coord, goal)
    ret.append(direction)

    assert len(path) == len(ret)

    return ret


def get_path_direction_list2(network, start, goal, path):
    ret = []

    beg_coord = start
    for from_seg, to_seg in zip(path, path[1:]):
        direction, beg_coord = get_path_seg_direction(network, beg_coord,
                                                      from_seg, to_seg)
        ret.append(direction)

    beg_coord = goal
    from_seg = path[-1]
    to_seg = path[-2]
    direction, _ = get_path_seg_direction(network, beg_coord, from_seg, to_seg)
    # direction = not direction
    ret.append(direction)

    assert len(path) == len(ret)
    return ret


def get_path_polyline(network, start, goal, path):
    directions = get_path_direction_list(network, start, goal, path)
    coords = []
    for idx in range(len(path)):
        buffer_coords(network, coords, path, directions, idx)

    polyline = LineString(coords)
    path_line = polyline

    # path_line = path_line.simplify(0.1, preserve_topology=True)
    path_line = path_line.simplify(0.1)
    return path_line


def move_to_origin(line):
    offsets = line.coords[0]
    line = affinity.translate(line, xoff=-offsets[0], yoff=-offsets[1])
    return line


def rotate_to_horizon(line):
    angle = math.atan2(line.coords[1][1], line.coords[1][0])
    angle = math.degrees(angle)
    line = affinity.rotate(line, -angle, origin=(0, 0))
    return line


def normalise_line(line):
    line = move_to_origin(line)
    line = rotate_to_horizon(line)
    return line


def get_normalised_point_difference(a_line, b_line):
    diff = 0.0

    a_line = normalise_line(a_line)
    b_line = normalise_line(b_line)

    for a_coord in a_line.coords:
        a_point = Point(*a_coord)
        diff += a_point.distance(b_line)

    return diff


class RoadTest:

    @staticmethod
    def to_dict(test):
        ret = dict()
        ret['test_id'] = test.test_id
        ret['network'] = NetworkLayout.to_dict(test.network)
        if test.start:
            ret['start'] = [test.start.x, test.start.y]
        if test.goal:
            ret['goal'] = [test.goal.x, test.goal.y]
        if test.score:
            ret['score'] = test.score
        if test.execution:
            ret['execution'] = TestExecution.to_dict(test.execution)
        if test.path:
            path_nodes = [segment.seg_id for segment in test.path]
            ret['path'] = path_nodes
        return ret

    @staticmethod
    def from_dict(dict):
        test_id = dict['test_id']
        network = NetworkLayout.from_dict(dict['network'])
        start = None
        if 'start' in dict:
            start = Point(dict['start'][0], dict['start'][1])
        goal = None
        if 'goal' in dict:
            goal = Point(dict['goal'][0], dict['goal'][1])

        test = RoadTest(test_id, network, start, goal)

        if 'path' in dict:
            path_nodes = dict['path']
            path = []
            for path_node in path_nodes:
                segment = network.nodes[path_node]
                path.append(segment)
            test.set_path(path)

        if 'score' in dict:
            test.score = dict['score']

        if 'execution' in dict:
            execution = TestExecution.from_dict(test, dict['execution'])
            test.execution = execution

        return test

    @staticmethod
    def get_suite_seg_distribution(tests, window):
        distribution = {}
        for test in tests:
            test_dist = test.get_seg_distribution(window)
            for key, val in test_dist.items():
                if key not in distribution:
                    distribution[key] = 0
                distribution[key] += val
        return distribution

    @staticmethod
    def get_suite_coverage(tests, window):
        distribution = RoadTest.get_suite_seg_distribution(tests, window)
        coverage = len(distribution.keys()) / seg_combination_count(window)
        return coverage

    @staticmethod
    def get_suite_total_distribution(tests, window):
        distribution = {}
        for test in tests:
            test_dist = test.get_full_seg_distribution(window)
            for key, val in test_dist.items():
                if key not in distribution:
                    distribution[key] = 0
                distribution[key] += val
        return distribution

    @staticmethod
    def get_suite_total_coverage(tests, window):
        distribution = RoadTest.get_suite_total_distribution(tests, window)
        coverage = len(distribution.keys()) / seg_combination_count(window)
        return coverage

    def __init__(self, test_id, network, start, goal):
        self.test_id = test_id
        self.network = network
        self.start = start
        self.goal = goal
        self.score = -1
        self.reason = None
        self.execution = None

        self.path = None
        self.path_line = None

    def __hash__(self):
        return hash(self.test_id)

    def __eq__(self, other):
        if isinstance(other, type(self)):
            return self.test_id == other.test_id

        return False

    def __str__(self):
        return 'Test#{}'.format(self.test_id)

    def set_start_goal(self, start, goal):
        self.start = start
        self.goal = goal
        self.path = None
        self.path_line = None
        self.seg_dist = None

    def set_path(self, path):
        if not self.start or not self.goal:
            raise ValueError('start or goal not set.')
        if not path:
            raise ValueError('Given path is empty')

        beg_poly = path[0].abs_polygon
        if not beg_poly.contains(self.start):
            raise ValueError('start coord not in beginning of path')

        end_poly = path[-1].abs_polygon
        if not end_poly.contains(self.goal):
            raise ValueError('goal coord not in ending of path')

        self.path = path
        self.path_line = None
        self.seg_dist = self.get_seg_distribution(3)

    def get_path(self):
        return self.path

    def get_path_polyline(self):
        if not self.path_line:
            path = self.get_path()
            self.path_line = get_path_polyline(self.network, self.start,
                                               self.goal, path)

            if self.start.geom_type != 'Point':
                raise ValueError('Not a point!')
            start_proj = self.path_line.project(self.start, normalized=True)
            start_proj = self.path_line.interpolate(
                start_proj, normalized=True)
            _, self.path_line = split(self.path_line, start_proj)

            if self.goal.geom_type != 'Point':
                raise ValueError('Not a point!')
            goal_proj = self.path_line.project(self.goal, normalized=True)
            goal_proj = self.path_line.interpolate(goal_proj, normalized=True)
            self.path_line, _ = split(self.path_line, goal_proj)

            l.info('Computed path polyline for test: %s', str(self))

        return self.path_line

    def copy(self, test_id=None):
        if not test_id:
            test_id = self.test_id

        ret = RoadTest(test_id, self.network.copy(), self.start, self.goal)
        return ret

    def get_path_difference(self, oth):
        own_path = self.path
        oth_path = oth.path

        difference = abs(len(own_path) - len(oth_path))
        for own_seg, oth_seg in zip(own_path, oth_path):
            if own_seg.key != oth_seg.key:
                difference += 1
        return difference

        own_poly = self.get_path_polyline()
        oth_poly = oth.get_path_polyline()

        diff = get_normalised_point_difference(own_poly, oth_poly)
        l.info('Determined path difference to be: %s', diff)
        return diff

    def get_section_keys(self, section):
        key = ''
        key += section[0].key
        if len(section) > 1:
            for i in range(len(section) - 1):
                from_node, to_node = section[i:i + 2]
                if self.network.is_intersecting_pair(from_node, to_node):
                    key += ' x '
                else:
                    key += ' > '
                key += to_node.key
        return key

    def get_path_seg_distribution(self, window, path):
        if not self.path:
            return None
        if len(self.path) < window:
            return None
        ret = dict()
        for i in range(len(path) - window + 1):
            section = path[i:i + window]
            section_key = self.get_section_keys(section)
            if section_key not in ret:
                ret[section_key] = 0
            ret[section_key] += 1

        return ret

    def get_seg_distribution(self, window):
        if not self.path:
            return None
        if len(self.path) < window:
            return None

        return self.get_path_seg_distribution(window, self.path)

    def get_full_seg_distribution(self, window):
        distribution = defaultdict(int)
        candidates = self.network.get_start_goal_candidates()
        if candidates:
            for start, goal in candidates:
                paths = self.network.all_paths(start, goal)
                for path in paths:
                    path_dist = self.get_path_seg_distribution(window, path)
                    distribution.update(path_dist)
            return distribution
        return None

    def similarity(self, other, window=2):
        slf_dist = self.get_seg_distribution(window)
        oth_dist = other.get_seg_distribution(window)
        slf_dist = set(slf_dist.keys())
        oth_dist = set(oth_dist.keys())

        shared = slf_dist.intersection(oth_dist)
        union = slf_dist.union(oth_dist)
        similarity = len(shared) / len(union)

        return similarity

    def distance(self, other, window=2):
        similarity = self.similarity(other, window)
        distance = 1 - similarity
        assert distance >= 0 and distance <= 1
        return distance


class CarState:

    @staticmethod
    def to_dict(state):
        dic = {}
        dic['test'] = state.test.test_id
        dic['pos_x'] = state.pos_x
        dic['pos_y'] = state.pos_y
        dic['pos_z'] = state.pos_z
        dic['steering'] = state.steering
        dic['vel_x'] = state.pos_x
        dic['vel_y'] = state.pos_y
        dic['vel_z'] = state.pos_z
        return dic

    @staticmethod
    def from_dict(test, state_dict):
        default = defaultdict(float)
        default.update(state_dict)
        state_dict = default
        state = CarState(test, state_dict['pos_x'], state_dict['pos_y'], state_dict['pos_z'],
                         0, state_dict['steering'], state_dict['vel_x'], state_dict['vel_y'], state_dict['vel_z'])
        return state

    def __init__(self, test, pos_x, pos_y, pos_z, damage, steering, vel_x,
                 vel_y, vel_z):
        self.test = test
        self.pos_x = pos_x
        self.pos_y = pos_y
        self.pos_z = pos_z
        self.damage = damage
        self.steering = steering
        self.vel_x = vel_x
        self.vel_y = vel_y
        self.vel_z = vel_z

    def get_path_projection(self):
        path = self.test.get_path_polyline()
        point = Point(self.pos_x, self.pos_y)
        if point.geom_type != 'Point':
            l.error('Point is: %s', point.geom_type)
            raise ValueError('Not a point!')
        proj = path.project(point, normalized=True)
        proj = path.interpolate(proj, normalized=True)
        return proj

    def get_segment(self):
        network = self.test.network
        pos = Point(self.pos_x, self.pos_y)
        nodes = network.get_nodes_at(pos)
        if nodes:
            return nodes.pop()
        else:
            return None

    def get_speed(self):
        return math.sqrt(self.vel_x ** 2 + self.vel_y ** 2)

    def get_centre_distance(self):
        proj = self.get_path_projection()
        pos = Point(self.pos_x, self.pos_y)
        distance = math.fabs(pos.distance(proj))

        network = self.test.network
        nodes = network.get_nodes_at(pos)
        if nodes:
            distance -= c.ev.lane_width / 2.0
        else:
            distance += c.ev.lane_width / 2.0

        distance = math.fabs(distance)
        return distance

    def get_spine_projection(self):
        point = Point(self.pos_x, self.pos_y)
        segments = self.test.network.get_nodes_at(point)
        if segments:
            segment = segments.pop()
            spine = segment.get_spine()
            if point.geom_type != 'Point':
                l.error('Point is: %s', point.geom_type)
                raise ValueError('Not a point!')
            proj = spine.project(point, normalized=True)
            proj = spine.interpolate(proj, normalized=True)
            return proj
        return None


class TestExecution:
    @staticmethod
    def to_dict(execution):
        dic = {}
        dic['test'] = execution.test.test_id
        dic['result'] = execution.result
        dic['reason'] = execution.reason

        states = []
        for state in execution.states:
            state_dic = CarState.to_dict(state)
            states.append(state_dic)
        dic['states'] = states

        dic['oobs'] = execution.oobs

        dic['start_time'] = execution.start_time.isoformat()
        dic['end_time'] = execution.end_time.isoformat()

        dic['minimum_distance'] = execution.minimum_distance
        dic['average_distance'] = execution.average_distance
        dic['maximum_distance'] = execution.maximum_distance

        dic['seg_oob_count'] = execution.seg_oob_count
        dic['oob_speeds'] = execution.oob_speeds

        return dic

    @staticmethod
    def from_dict(test, execution_dict):
        states = []
        for state_dict in execution_dict['states']:
            state = CarState.from_dict(test, state_dict)
            states.append(state)
        if 'oobs' not in execution_dict:
            execution_dict['oobs'] = 0
        exec = TestExecution(test, execution_dict['result'], execution_dict['reason'], states, execution_dict['oobs'],
                             dateutil.parser.parse(
                                 execution_dict['start_time']),
                             dateutil.parser.parse(execution_dict['end_time']))

        if 'minimum_distance' in execution_dict:
            exec.minimum_distance = execution_dict['minimum_distance']
        if 'average_distance' in execution_dict:
            exec.average_distance = execution_dict['average_distance']
        if 'maximum_distance' in execution_dict:
            exec.maximum_distance = execution_dict['maximum_distance']

        if 'seg_oob_count' in execution_dict:
            exec.seg_oob_count = execution_dict['seg_oob_count']
        if 'oob_speeds' in execution_dict:
            exec.oob_speeds = execution_dict['oob_speeds']
        if 'oobs' in execution_dict:
            exec.oobs = execution_dict['oobs']

        return exec

    def __init__(self, test, result, reason, states, oobs, start_time, end_time,
                 **options):
        self.test = test
        self.result = result
        self.reason = reason
        self.states = states
        self.oobs = oobs
        self.start_time = start_time
        self.end_time = end_time

        self.minimum_distance = options.get('minimum_distance', None)
        self.average_distance = options.get('average_distance', None)
        self.maximum_distance = options.get('maximum_distance', None)

        self.seg_oob_count = None
        self.oob_speeds = None

    def off_track(self, carstate):
        distance = carstate.get_centre_distance()
        if distance > c.ev.lane_width / 2.0:
            return True

        return False

    def count_oobs(self):
        oob_lens = []
        cnt_oobs = 0
        is_oob = False
        cur_len = 0
        for state in self.states:
            is_off_track = self.off_track(state)
            if is_off_track:
                if not is_oob:
                    is_oob = True
                    cnt_oobs += 1
                cur_len += 1
            else:
                is_oob = False
                if cur_len > 0:
                    oob_lens.append(cur_len)
                    cur_len = 0

        return cnt_oobs, oob_lens

    def max_distance(self):
        max_dist = -1
        for state in self.states:
            centre_dist = state.get_centre_distance()
            if centre_dist > max_dist:
                max_dist = centre_dist
        return max_dist
