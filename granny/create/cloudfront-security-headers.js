function handler(event) {
    var response = event.response;
    var headers = response.headers;

    // Strict Transport Security - Force HTTPS for 1 year
    headers['strict-transport-security'] = {
        value: 'max-age=31536000; includeSubDomains; preload'
    };

    // Content Security Policy - Allow your domains and required resources
    headers['content-security-policy'] = {
        value: "default-src 'self' https:; " +
               "script-src 'self' 'unsafe-eval' 'unsafe-inline' https: blob:; " +
               "style-src 'self' 'unsafe-inline' https:; " +
               "img-src 'self' https: data: blob:; " +
               "font-src 'self' https: data:; " +
               "connect-src 'self' https: wss:; " +
               "media-src 'self' https: blob:; " +
               "object-src 'none'; " +
               "base-uri 'self'; " +
               "form-action 'self' https:; " +
               "frame-ancestors 'none'; " +
               "worker-src 'self' blob:; " +
               "manifest-src 'self';"
    };

    // Prevent MIME type sniffing
    headers['x-content-type-options'] = {
        value: 'nosniff'
    };

    // Prevent clickjacking
    headers['x-frame-options'] = {
        value: 'DENY'
    };

    // Control referrer information
    headers['referrer-policy'] = {
        value: 'strict-origin-when-cross-origin'
    };

    // Permissions Policy - Disable sensitive APIs by default
    headers['permissions-policy'] = {
        value: 'geolocation=(), microphone=(), camera=(), payment=(), usb=(), magnetometer=(), gyroscope=()'
    };

    // X-XSS-Protection (legacy but still useful)
    headers['x-xss-protection'] = {
        value: '1; mode=block'
    };

    return response;
} 