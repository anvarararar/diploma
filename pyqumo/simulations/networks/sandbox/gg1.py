from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Sequence, Any
import numpy as np

from pyqumo.randoms import CountableDistribution, Distribution
from pyqumo.stats import TimeSizeRecords, build_statistics

from ..contract.gg1_contract import GG1Results
from .components import FiniteFifoQueue, InfiniteFifoQueue, Server, Queue


def simulate_gg1(
        arrival: Distribution,
        service: Distribution,
        queue_capacity: int = np.inf,
        max_time: float = np.inf,
        max_packets: int = 1000000
) -> GG1Results:
    """
    Run simulation model of G/G/1/N system.

    Simulation can be stopped in two ways: by reaching maximum simulation time,
    or by reaching the maximum number of generated packets. By default,
    simulation is limited with the maximum number of packets only (1 million).

    Queue is expected to have finite capacity.

    Arrival and service time processes can be of any kind, including Poisson
    or MAP. To use a PH or normal distribution, a GenericIndependentProcess
    model with the corresponding distribution may be used.

    Parameters
    ----------
    arrival : RandomProcess
        Arrival random process.
    service : RandomProcess
        Service time random process.
    queue_capacity : int
        Queue capacity.
    max_time : float, optional
        Maximum simulation time (default: infinity).
    max_packets
        Maximum number of simulated packets (default: 1'000'000)

    Returns
    -------
    results : Results
        Simulation results.
    """
    params = _Params(
        arrival=arrival.rnd, service=service.rnd, queue_capacity=queue_capacity,
        max_packets=max_packets, max_time=max_time)
    system = _System(params)
    records = _Records()

    # Initialize model:
    records.system_size.add(0.0, system.size)
    system.schedule(_Event.ARRIVAL, params.arrival.eval())

    # Run simulation:
    max_time = params.max_time
    while not system.stopped:
        event = system.next_event()

        # Check whether event is scheduled too late, and we need to stop:
        if system.time > max_time:
            system.stop()
            continue

        # Process event
        if event == _Event.ARRIVAL:
            _handle_arrival(system, params, records)
        elif event == _Event.SERVICE_END:
            _handle_service_end(system, params, records)
        elif event == _Event.STOP:
            system.stop()

    return _build_results(records)


@dataclass
class _Packet:
    """
    Packet representation for G/G/1/N model.

    Stores timestamps: when the packet arrived (created_at), started
    serving (service_started_at) and finished serving (departed_at).

    Also stores flags, indicating whether the packet was dropped or was
    completely served. Packets arrived in the end of the modeling, may not
    be served, as well as dropped packets.
    """
    created_at: float
    service_started_at: Optional[float] = None
    departed_at: Optional[float] = None
    dropped: bool = False
    served: bool = False


class _Records:
    """
    Records for G/G/1/N statistical analysis.
    """
    def __init__(self):
        self._packets: List[_Packet] = []
        self._system_size = TimeSizeRecords()

    def add_packet(self, packet: _Packet):
        self._packets.append(packet)

    @property
    def system_size(self):
        return self._system_size

    @property
    def packets(self):
        return self._packets


@dataclass
class _Params:
    """
    Model parameters: arrival and service processes, queue capacity and limits.
    """
    arrival: Any
    service: Any
    queue_capacity: int
    max_packets: int = 1000000
    max_time: float = np.inf


class _Event(Enum):
    STOP = 0
    ARRIVAL = 1
    SERVICE_END = 2


class _System:
    """
    System state representation.

    This object takes care of the queue, current time, next arrival and
    service end time, server status and any other kind of dynamic information,
    except for internal state of arrival or service processes.
    """
    def __init__(self, params: _Params):
        """
        Constructor.

        Parameters
        ----------
        params : Params
            Model parameters
        """
        if params.queue_capacity < np.inf:
            self._queue = FiniteFifoQueue(params.queue_capacity)
        else:
            self._queue = InfiniteFifoQueue()

        self._time: float = 0.0
        self._service_end: Optional[float] = None
        self._next_arrival: Optional[float] = 0.0
        self._server: Server[_Packet] = Server()
        self._stopped: bool = False

    @property
    def server(self) -> Server[_Packet]:
        return self._server

    @property
    def queue(self) -> Queue[_Packet]:
        return self._queue

    @property
    def time(self) -> float:
        return self._time

    @property
    def stopped(self):
        return self._stopped

    @property
    def size(self) -> int:
        """
        Get system size, that is queue size plus one (busy) or zero (empty).
        """
        return self.server.size + self.queue.size

    def schedule(self, event: _Event, interval: float) -> None:
        """
        Schedule next event.

        Parameters
        ----------
        event : Event
        interval : float
            Non-negative interval, after which the event will be fired.
        """
        if interval < 0:
            raise ValueError(f"expected non-negative interval, but "
                             f"{interval} found")
        if event == _Event.ARRIVAL:
            self._next_arrival = self._time + interval
        elif event == _Event.SERVICE_END:
            self._service_end = self._time + interval
        else:
            raise ValueError(f"unexpected event {event}")

    def stop(self) -> None:
        self._stopped = True

    def next_event(self) -> _Event:
        """
        Get next event type and move time to it.

        Returns
        -------
        event : Event
        """
        ts = self._service_end
        ta = self._next_arrival

        if ts is None and ta is None:
            return _Event.STOP

        if ts is not None and (ta is None or ta > ts):
            self._time = ts
            self._service_end = None
            return _Event.SERVICE_END

        # If we are here, TS is None, or TS is not None and NOT (ta > ts):
        # this means, that arrival happens
        self._time = self._next_arrival
        self._next_arrival = None
        return _Event.ARRIVAL


def _handle_arrival(system: _System, params: _Params, records: _Records):
    """
    Handle new packet arrival event.

    First of all, a new packet is created. Then we check whether the
    system is empty. If it is, this new packet starts serving immediately.
    Otherwise, it is added to the queue.

    If the queue was full, the packet is dropped. To mark this, we set
    `dropped` flag in the packet to `True`.

    In the end we schedule the next arrival. We also check whether the
    we have already generated enough packets. If so, `system.stopped` flag
    is set to `True`, so on the next main loop iteration the simulation
    will be stopped.

    Parameters
    ----------
    system : System
    params : Params
    records : Records
    """
    num_packets_built = len(records.packets)

    # If too many packets were generated, ask to stop:
    if num_packets_built >= params.max_packets:
        system.stop()

    time_now = system.time
    packet = _Packet(created_at=time_now)
    records.add_packet(packet)

    # If server is ready, start serving. Otherwise, push the packet into
    # the queue. If the queue was full, mark the packet is being dropped
    # for further analysis.
    server = system.server
    if server.ready:
        # start serving immediately
        server.serve(packet)
        system.schedule(_Event.SERVICE_END, params.service.eval())
        packet.service_started_at = time_now
        records.system_size.add(time_now, system.size)

    elif system.queue.push(packet):
        # packet was queued
        records.system_size.add(time_now, system.size)

    else:
        # mark packet as being dropped
        packet.dropped = True

    # Schedule next arrival:
    system.schedule(_Event.ARRIVAL, params.arrival.eval())


def _handle_service_end(system: _System, params: _Params, records: _Records):
    """
    Handle end of the packet service.

    If the queue is empty, the server becomes idle. Otherwise, it starts
    serving the next packet from the queue.

    The packet that left the server is marked as `served = True`.

    Parameters
    ----------
    system : System
    params : Params
    records : Records
    """
    time_now = system.time
    server = system.server
    queue = system.queue

    packet = server.pop()
    packet.served = True
    packet.departed_at = time_now

    # Start serving next packet, if exists:
    packet = queue.pop()
    if packet is not None:
        server.serve(packet)
        packet.service_started_at = time_now
        system.schedule(_Event.SERVICE_END, params.service.eval())

    # Anyway, system size has changed - record it!
    records.system_size.add(time_now, system.size)


def _get_departure_intervals(packets_list: Sequence[_Packet]) -> List[float]:
    """
    Build departures intervals sequence.

    Parameters
    ----------
    packets_list : sequence of Packet

    Returns
    -------
    intervals : sequence of float
    """
    prev_time = 0.0
    intervals = []
    for packet in packets_list:
        if packet.served:
            intervals.append(packet.departed_at - prev_time)
            prev_time = packet.departed_at
    return intervals


def _build_results(records: _Records) -> GG1Results:
    """
    Create results from the records.

    Parameters
    ----------
    records : Records
    """
    ret = GG1Results()

    #
    # 1) Build system size, queue size and busy (server size)
    #    distributions. To do this, we need PMFs. Queue size
    #    PMF and busy PMF can be computed from system size PMF.
    #
    system_size_pmf = list(records.system_size.pmf)
    num_states = len(system_size_pmf)
    p0 = system_size_pmf[0]
    p1 = system_size_pmf[1] if num_states > 1 else 0.0
    queue_size_pmf = [p0 + p1] + system_size_pmf[2:]
    server_size_pmf = [p0, sum(system_size_pmf[1:])]

    ret.system_size = CountableDistribution(system_size_pmf)
    ret.queue_size = CountableDistribution(queue_size_pmf)
    ret.busy = CountableDistribution(server_size_pmf)

    #
    # 2) For future estimations, we need packets and some filters.
    #    Group all of them here.
    #
    all_packets = records.packets
    served_packets = [packet for packet in all_packets if packet.served]
    dropped_packets = [packet for packet in all_packets if packet.dropped]

    #
    # 3) Build scalar statistics.
    #
    ret.loss_prob = len(dropped_packets) / len(all_packets)

    #
    # 4) Build various intervals statistics: departures, waiting times,
    #    response times.
    #
    departure_intervals = _get_departure_intervals(served_packets)
    ret.departures = build_statistics(np.asarray(departure_intervals))
    ret.response_time = build_statistics([
        pkt.departed_at - pkt.created_at for pkt in served_packets
    ])
    ret.wait_time = build_statistics([
        pkt.service_started_at - pkt.created_at for pkt in served_packets
    ])

    return ret
