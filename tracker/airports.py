"""Airports the tools can place on a globe, and offer for autocomplete.

A starter list of common origins and connecting hubs. Add whatever your routes
actually use: a routing through an airport that is missing is dropped and
named rather than blanking the viewer, so extending this is a one-line fix
when you see the warning.
"""

AP = {
 "AMS":("Amsterdam","Netherlands",52.3105,4.7683),  "ATH":("Athens","Greece",37.9364,23.9445),
 "ADL":("Adelaide","Australia",-34.9285,138.5304),
 "AKL":("Auckland","New Zealand",-37.0082,174.7850),"AUH":("Abu Dhabi","UAE",24.4330,54.6511),
 "BCN":("Barcelona","Spain",41.2974,2.0833),        "BKK":("Bangkok","Thailand",13.6900,100.7501),
 "BNE":("Brisbane","Australia",-27.3842,153.1175),  "BOM":("Mumbai","India",19.0896,72.8656),
 "CAN":("Guangzhou","China",23.3924,113.2988),      "CDG":("Paris","France",49.0097,2.5479),
 "CGK":("Jakarta","Indonesia",-6.1256,106.6559),    "CMB":("Colombo","Sri Lanka",7.1808,79.8841),
 "CPT":("Cape Town","South Africa",-33.9715,18.6021),"DAD":("Da Nang","Vietnam",16.0439,108.1994),
 "DEL":("Delhi","India",28.5562,77.1000),           "DOH":("Doha","Qatar",25.2731,51.6080),
 "DPS":("Denpasar","Indonesia",-8.7482,115.1672),   "DXB":("Dubai","UAE",25.2532,55.3657),
 "FCO":("Rome","Italy",41.8003,12.2389),            "FRA":("Frankfurt","Germany",50.0379,8.5622),
 "HAN":("Hanoi","Vietnam",21.2212,105.8072),        "HKG":("Hong Kong","China",22.3080,113.9185),
 "HND":("Tokyo Haneda","Japan",35.5494,139.7798),   "IST":("Istanbul","Turkey",41.2753,28.7519),
 "ICN":("Seoul","South Korea",37.4602,126.4407),    "JED":("Jeddah","Saudi Arabia",21.6796,39.1565),
 "JFK":("New York","USA",40.6413,-73.7781),         "JNB":("Johannesburg","South Africa",-26.1367,28.2411),
 "KIX":("Osaka","Japan",34.4273,135.2440),          "KUL":("Kuala Lumpur","Malaysia",2.7456,101.7099),
 "KWI":("Kuwait City","Kuwait",29.2266,47.9689),    "LAX":("Los Angeles","USA",33.9416,-118.4085),
 "LHR":("London","UK",51.4700,-0.4543),             "MAD":("Madrid","Spain",40.4839,-3.5680),
 "MCT":("Muscat","Oman",23.5933,58.2844),           "MEL":("Melbourne","Australia",-37.6690,144.8410),
 "MNL":("Manila","Philippines",14.5086,121.0194),   "MUC":("Munich","Germany",48.3538,11.7861),
 "NRT":("Tokyo Narita","Japan",35.7720,140.3929),   "PEK":("Beijing","China",40.0799,116.6031),
 "PEN":("Penang","Malaysia",5.2971,100.2769),       "PER":("Perth","Australia",-31.9385,115.9672),
 "PRN":("Pristina","Kosovo",42.5728,21.0358),       "PVG":("Shanghai","China",31.1443,121.8083),
 "RUH":("Riyadh","Saudi Arabia",24.9576,46.6988),   "SFO":("San Francisco","USA",37.6213,-122.3790),
 "SGN":("Ho Chi Minh City","Vietnam",10.8188,106.6520),"SIN":("Singapore","Singapore",1.3644,103.9915),
 "SYD":("Sydney","Australia",-33.9399,151.1753),    "SZX":("Shenzhen","China",22.6393,113.8108),
 "TPE":("Taipei","Taiwan",25.0777,121.2328),        "VIE":("Vienna","Austria",48.1103,16.5697),
 "XMN":("Xiamen","China",24.5440,118.1276),         "YVR":("Vancouver","Canada",49.1967,-123.1815),
 "YYZ":("Toronto","Canada",43.6777,-79.6248),       "ZRH":("Zurich","Switzerland",47.4647,8.5492),
}


def choices():
    """[{code, city, country}] for a picker, sorted by city."""
    return sorted(({"code": k, "city": v[0], "country": v[1]} for k, v in AP.items()),
                  key=lambda x: x["city"])
