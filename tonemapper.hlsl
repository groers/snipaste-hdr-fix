Texture2D<float4> src : register(t0);
RWTexture2D<float4> dest : register(u0);

cbuffer data : register(b0)
{
	float white_level;
	uint is_hdr;
	float3x3 transform;
}

float3 soft_clip(float3 x)
{
	return saturate((1.0 + x - sqrt(1.0 - 1.99 * x + x * x)) / (1.995));
}

float3 linear_tonemap(float3 x)
{
	const float z = 1.0;
	const float d = 2.5;

	return lerp(x, (x - z) / d + z, step(z, x));
}

float3 bt2020_inv_gamma(float3 x)
{
	return (x > 0.00313066844250063) ? 1.055 * pow(clamp(x, 0, 10000), 1.0 / 2.4) - 0.055 : 12.92 * x;
}

float rgb_to_luma(float3 x)
{
	return dot(float3(0.213, 0.715, 0.072), x);
}

// Khronos PBR Neutral Tone Mapper
// https://github.com/KhronosGroup/ToneMapping/tree/main/PBR_Neutral

// Input color is non-negative and resides in the Linear Rec. 709 color space.
// Output color is also Linear Rec. 709, but in the [0, 1] range.
float3 neutral(float3 color)
{
	const float startCompression = 0.8 - 0.04;
	const float desaturation = 0.15;

	float x = min(color.r, min(color.g, color.b));
	float offset = x < 0.08 ? x - 6.25 * x * x : 0.04;
	color -= offset;

	float peak = max(color.r, max(color.g, color.b));
	float3 result = color;
	
	if (peak >= startCompression)
	{
		const float d = 1.0 - startCompression;
		float newPeak = 1.0 - d * d / (peak + d - startCompression);
		color *= newPeak / peak;

		float g = 1.0 - 1.0 / (desaturation * (peak - newPeak) + 1.0);
		result = lerp(color, newPeak.xxx, g);
	}
	
	return result;
}

uint2 calc_dest_pos(float2 src, uint width, uint height)
{
    float2x2 rotation =
    {
        transform._11, transform._12,
        transform._21, transform._22,
    };

    float2 center = float2(width, height) / 2.0;
    float2 src_center = src + float2(0.5, 0.5);

    float2 transformed = mul(float3(src_center - center, 1), transform).xy;
    float2 rotated_zero = mul(-center, rotation);

    transformed += abs(rotated_zero);
    transformed -= float2(0.5, 0.5);

    return uint2(round(transformed));
}

[numthreads(16, 16, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
	uint width, height;
	src.GetDimensions(width, height);

	if (tid.x > width || tid.y > height)
	{
		return;
	}

	uint2 src_pos = tid.xy;
	uint2 dest_pos = calc_dest_pos(src_pos, width, height);
    
	float3 src_color = src[src_pos].rgb;
	
	if (is_hdr == 1)
	{
		float3 input_color = clamp(src_color, 0, 10000) / (white_level / 80);
		float3 linear_color = bt2020_inv_gamma(input_color);

		float3 linear_result = linear_tonemap(linear_color);
		float3 neutral_result = neutral(linear_color);

		float linear_luma = rgb_to_luma(linear_result);
		float neutral_luma = rgb_to_luma(neutral_result);
		float3 neutral_color = neutral_result / neutral_luma;

		linear_color = lerp(linear_result, neutral_color * linear_luma, step(1.0, linear_luma));

		dest[dest_pos] = float4(linear_color, 1.0);
	}
	else
	{
		dest[dest_pos] = float4(src_color, 1.0);
	}
}
