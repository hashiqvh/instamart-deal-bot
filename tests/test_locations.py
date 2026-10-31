from instadeals.locations import maps_location

def test_place_marker_not_viewport():
    assert maps_location('https://www.google.com/maps/place/DLF+New+Town+Heights/@11,77,952m/data=!3d10.0107421!4d76.3442586') == ('DLF New Town Heights',10.010742,76.344259)

def test_reject_viewport_short_and_untrusted():
    for url in ['https://www.google.com/maps/@10,76,10z','https://maps.app.goo.gl/test','https://evil.com/maps/data=!3d10!4d76','https://www.google.com/maps/data=!3d100!4d76']:
        assert maps_location(url) is None

def test_query_coordinates():
    assert maps_location('https://www.google.com/maps/search/?api=1&query=10.01%2C76.34')==('Maps location',10.01,76.34)
