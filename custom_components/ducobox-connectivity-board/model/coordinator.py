from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.core import HomeAssistant
from ..const import SCAN_INTERVAL
from .devices import DucoboxSensorEntityDescription, DucoboxNodeSensorEntityDescription
from .utils import safe_get
from typing import Any
from ducopy import DucoPy
from ducopy.rest.models import ConfigNodeRequest
import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
import time
import json
from datetime import timedelta
from requests.adapters import HTTPAdapter


_LOGGER = logging.getLogger(__name__)


class TimeoutHTTPAdapter(HTTPAdapter):
    """HTTPAdapter with default timeout."""

    def __init__(self, timeout, *args, **kwargs):
        self.timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        kwargs.setdefault('timeout', self.timeout)
        return super().send(request, **kwargs)

class DucoboxCoordinator(DataUpdateCoordinator):
    """Coordinator to manage data updates for Ducobox sensors."""

    def __init__(self, hass: HomeAssistant, duco_client: DucoPy):
        super().__init__(
            hass,
            _LOGGER,
            name="Ducobox Connectivity Board",
            update_interval=SCAN_INTERVAL,
        )

        # Add timeout adapter to prevent coordinator stalling
        # The ducopy library doesn't support timeout parameters, so we add it via adapter
        timeout_seconds = (SCAN_INTERVAL - timedelta(seconds=5)).total_seconds()
        adapter = TimeoutHTTPAdapter(timeout=timeout_seconds)
        duco_client.client.session.mount("http://", adapter)
        duco_client.client.session.mount("https://", adapter)

        self.duco_client = duco_client
        self._static_data = None

    async def _async_update_data(self) -> dict:
        """Fetch data from the Ducobox API."""
        try:
            return await self.hass.async_add_executor_job(self._fetch_data)
        except Exception as e:
            _LOGGER.error("Failed to fetch data from Ducobox API: %s", e)
            raise UpdateFailed(f"Failed to fetch data from Ducobox API: {e}") from e

    async def _async_setup(self) -> None:
        """Do initialization logic."""
        self._static_data = await self.hass.async_add_executor_job(self._fetch_once_data)

    def _fetch_once_data(self) -> dict:
        data = {}
        node_actions = self.duco_client.raw_get('/action/nodes')
        data['action_nodes'] = node_actions
        _LOGGER.debug(f"Data received from /action/nodes = {node_actions}")

        return data

    def _fetch_data(self) -> dict:
        duco_client = self.duco_client

        data = {}

        if duco_client is None:
            raise Exception("Duco client is not initialized")

        try:
            data['info'] = duco_client.get_info()
            _LOGGER.debug(f"Data received from /info: {data}")

            nodes_response = duco_client.get_nodes()
            _LOGGER.debug(f"Data received from /info/nodes: {nodes_response}")

            if nodes_response and hasattr(nodes_response, 'Nodes'):
                data['nodes'] = [node.dict() for node in nodes_response.Nodes]
            else:
                data['nodes'] = []

            config_nodes = duco_client.raw_get('/config/nodes')
            data['config_nodes'] = config_nodes
            _LOGGER.debug(f"Data received from /config/nodes = {data['config_nodes']}")

            data['mappings'] = {'node_id_to_name': {}, 'node_id_to_type': {}}
            for node in data['nodes']:
                node_id = node.get('Node')
                node_type = safe_get(node, 'General', 'Type', 'Val') or 'Unknown'
                node_name = f"{node_id}:{node_type}"

                data['mappings']['node_id_to_name'][node_id] = node_name
                data['mappings']['node_id_to_type'][node_id] = node_type


            return {**data, **self._static_data}
        except Exception as e:
            _LOGGER.error("Error fetching data from Ducobox API: %s", e)
            raise e

    async def async_set_value(self, node_id, key, value):
        """Send an update to the device."""
        try:
            data = json.dumps({
                key: {'Val': int(round(value, 0))},
            }, separators=(',', ':'))

            logging.error(str(data))
            logging.error(f'/config/nodes/{node_id}')
            # Use the DucoPy client to update the configuration
            await self.hass.async_add_executor_job(
                self.duco_client.raw_patch, f'/config/nodes/{node_id}', data
            )

            _LOGGER.info(f"Successfully set value for node {node_id}, key {key} to {value}")
        except Exception as e:
            _LOGGER.error(f"Failed to set value for node {node_id}, key {key}: {e}")
            raise

    async def async_set_ventilation_state(self, node_id, option, action):
        try:
            await self.hass.async_add_executor_job(
                self.duco_client.change_action_node, action, option, node_id
            )
            
            _LOGGER.info(f"Successfully set config value for node {node_id}, action {action} to {option}")
        except Exception as e:
            _LOGGER.error(f"Failed to set config value for node {node_id}, action {action}: {e}")
            raise

class DucoboxSensorEntity(CoordinatorEntity[DucoboxCoordinator], SensorEntity):
    """Representation of a Ducobox sensor entity."""
    entity_description: DucoboxSensorEntityDescription

    def __init__(
        self,
        coordinator: DucoboxCoordinator,
        description: DucoboxSensorEntityDescription,
        device_info: DeviceInfo,
        unique_id: str,
    ) -> None:
        """Initialize a Ducobox sensor entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_device_info = device_info
        self._attr_unique_id = unique_id
        self._attr_name = f"{device_info['name']} {description.name}"

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        try:
            return self.entity_description.value_fn(self.coordinator.data)
        except Exception as e:
            _LOGGER.debug(f"Error getting value for {self.name}: {e}")
            return None

class DucoboxNodeSensorEntity(CoordinatorEntity[DucoboxCoordinator], SensorEntity):
    """Representation of a Ducobox node sensor entity."""
    entity_description: DucoboxNodeSensorEntityDescription

    def __init__(
        self,
        coordinator: DucoboxCoordinator,
        node_id: int,
        description: DucoboxNodeSensorEntityDescription,
        device_info: DeviceInfo,
        unique_id: str,
        device_id: str,
        node_name: str,
    ) -> None:
        """Initialize a Ducobox node sensor entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_device_info = device_info
        self._attr_unique_id = unique_id
        self._node_id = node_id
        self._attr_name = f"{node_name} {description.name}"

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        nodes = self.coordinator.data.get('nodes', [])
        for node in nodes:
            if node.get('Node') == self._node_id:
                try:
                    return self.entity_description.value_fn(node)
                except Exception as e:
                    _LOGGER.debug(f"Error getting value for {self.name}: {e}")
                    return None
        return None
