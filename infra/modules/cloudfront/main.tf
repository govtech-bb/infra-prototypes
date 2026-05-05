resource "aws_cloudfront_origin_access_control" "this" {
  name                              = "${var.project_name}-${var.env}-oac"
  description                       = "OAC for ${var.project_name} ${var.env}"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = var.index_document
  price_class         = var.price_class

  origin {
    domain_name              = var.s3_bucket_regional_domain_name
    origin_id                = "S3Origin"
    origin_access_control_id = aws_cloudfront_origin_access_control.this.id
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "S3Origin"
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    min_ttl     = 0
    default_ttl = var.default_ttl
    max_ttl     = var.max_ttl
  }

  # SPA routing — redirect 403/404 back to index.html for client-side routing
  dynamic "custom_error_response" {
    for_each = var.is_spa ? [403, 404] : []
    content {
      error_code            = custom_error_response.value
      response_code         = 200
      response_page_path    = "/${var.index_document}"
      error_caching_min_ttl = 10
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = var.acm_certificate_arn == null ? true : false

    dynamic "acm_certificate" {
      for_each = var.acm_certificate_arn != null ? [1] : []
      content {
        acm_certificate_arn      = var.acm_certificate_arn
        ssl_support_method       = "sni-only"
        minimum_protocol_version = "TLSv1.2_2021"
      }
    }
  }

  aliases = var.domain_names

  tags = merge(var.tags, {
    Project     = var.project_name
    Environment = var.env
  })
}
