"""Constants for We Connect library."""

BASE_AUTH = "https://identity.vwgroup.io"
BASE_AUTH_NA = "https://identity.na.vwgroup.io"
BASE_SESSION = "https://msg.volkswagen.de"
BRAND = "VW"
COUNTRY = "DE"


# Data used in communication
CLIENT = {
    "Legacy": {
        "CLIENT_ID": "9496332b-ea03-4091-a224-8c746b885068@apps_vw-dilab_com",
        # client id for VWG API, legacy Skoda Connect/MySkoda
        "CLIENT_ID_NA": "720b402a-0348-489d-9ae9-eedbe24c1d88_MYVW_IOS",
        # client id for VWG API, legacy North America
        "SCOPE": "openid mbb profile cars address email birthdate nickname phone",
        # 'SCOPE': 'openid mbb profile cars address email birthdate badge phone driversLicense dealers profession vin',
        "TOKEN_TYPES": "code id_token token",
    },
    "New": {
        "CLIENT_ID": "f9a2359a-b776-46d9-bd0c-db1904343117@apps_vw-dilab_com",
        # Provides access to new API? tokentype=IDK_TECHNICAL..
        "SCOPE": "openid mbb profile",
        "TOKEN_TYPES": "code id_token",
    },
    "Unknown": {
        "CLIENT_ID": "72f9d29d-aa2b-40c1-bebe-4c7683681d4c@apps_vw-dilab_com",  # gives tokentype=IDK_SMARTLINK ?
        "SCOPE": "openid dealers profile email cars address",
        "TOKEN_TYPES": "code id_token",
    },
}

CLIENT_ID_NA = "b680e751-7e1f-4008-8ec1-3a528183d215"
XCLIENT_ID = "c8fcb3bf-22d3-44b0-b6ce-30eae0a4986f"
XAPPVERSION = "5.3.2"
XAPPNAME = "We Connect"
USER_AGENT = "okhttp/3.14.7"
APP_URI = "carnet://identity-kit/login"
APP_URI_NA = "kombi:///login"

# Used when fetching data
HEADERS_SESSION = {
    "Connection": "keep-alive",
    "Content-Type": "application/json",
    "Accept-charset": "UTF-8",
    "Accept": "application/json",
    "X-Client-Id": XCLIENT_ID,
    "X-App-Version": XAPPVERSION,
    "X-App-Name": XAPPNAME,
    "User-Agent": USER_AGENT,
    "tokentype": "IDK_TECHNICAL",
}

# Used when fetching data. North America
HEADERS_SESSION_NA = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-encoding": "gzip, deflate, br",
    #"sec-fetch-site": "none",
    #"sec-fetch-mode": "navigate",
    "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "accept-language": "en-US,en;q=0.9",
    #"sec-fetch-dest": "document"
}

# Used for authentication
HEADERS_AUTH = {
    "Connection": "keep-alive",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Content-Type": "application/x-www-form-urlencoded",
    "x-requested-with": XAPPNAME,
    "User-Agent": USER_AGENT,
    "X-App-Name": XAPPNAME,
}

TEMP_CELSIUS: str = "°C"
TEMP_FAHRENHEIT: str = "°F"


class VWStateClass:
    """Supported state classes."""

    MEASUREMENT = "measurement"
    TOTAL = "total"
    TOTAL_INCREASING = "total_increasing"


class VWDeviceClass:
    """Supported sensor entity device classes."""

    BATTERY = "battery"
    CONNECTIVITY = "connectivity"
    DOOR = "door"
    LIGHT = "light"
    LOCK = "lock"
    MOVING = "moving"
    PLUG = "plug"
    POWER = "power"
    TEMPERATURE = "temperature"
    WINDOW = "window"


class VehicleStatusParameter:
    """Hex codes for vehicle status parameters."""

    FRONT_LEFT_DOOR_LOCK = "0x0301040001"
    FRONT_RIGHT_DOOR_LOCK = "0x0301040007"
    REAR_LEFT_DOOR_LOCK = "0x0301040004"
    READ_RIGHT_DOOR_LOCK = "0x030104000A"

    FRONT_LEFT_DOOR_CLOSED = "0x0301040002"
    FRONT_RIGHT_DOOR_CLOSED = "0x0301040008"
    REAR_LEFT_DOOR_CLOSED = "0x0301040005"
    REAR_RIGHT_DOOR_CLOSED = "0x030104000B"

    HOOD_CLOSED = "0x0301040011"

    TRUNK_LOCK = "0x030104000D"
    TRUNK_CLOSED = "0x030104000E"

    FRONT_LEFT_WINDOW_CLOSED = "0x0301050001"
    FRONT_RIGHT_WINDOW_CLOSED = "0x0301050005"
    REAR_LEFT_WINDOW_CLOSED = "0x0301050003"
    REAR_RIGHT_WINDOW_CLOSED = "0x0301050007"
    SUNROOF_CLOSED = "0x030105000B"

    PRIMARY_RANGE = "0x0301030006"
    SECONDARY_RANGE = "0x0301030008"

    PRIMARY_DRIVE = "0x0301030007"
    SECONDARY_DRIVE = "0x0301030009"
    COMBINED_RANGE = "0x0301030005"
    FUEL_LEVEL = "0x030103000A"

    PARKING_LIGHT = "0x0301010001"

    ODOMETER = "0x0101010002"

    DAYS_TO_SERVICE_INSPECTION = "0x0203010004"
    DISTANCE_TO_SERVICE_INSPECTION = "0x0203010003"

    DAYS_TO_OIL_INSPECTION = "0x0203010002"
    DISTANCE_TO_OIL_INSPECTION = "0x0203010001"

    ADBLUE_LEVEL = "0x02040C0001"

    OUTSIDE_TEMPERATURE = "0x0301020001"
