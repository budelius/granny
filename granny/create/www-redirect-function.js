function handler(event) {
  var request = event.request;
  var host = request.headers.host.value;
  
  if (host.startsWith('www.')) {
    var newHost = host.substring(4);
    return {
      statusCode: 301,
      statusDescription: 'Moved Permanently',
      headers: {
        'location': { value: 'https://' + newHost + request.uri }
      }
    };
  }
  
  return request;
}
